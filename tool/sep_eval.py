# -*- coding: utf-8 -*-
"""sep_eval.py —— 分离评测 v2：**漏（残留）与伤（失真）同时量**。

为什么要重写
------------
上一版只量了"人声轨里还剩多少鼓"，于是把 Wiener 侧链一路调猛，
数字确实从 −20.8 dB 降到 −27.4 dB，但用户听完说"更差了"。
根因是那个指标**看不见损伤**：任何时变掩蔽都会在人声自己的谐波上
做幅度调制（掩蔽越强越明显），听感就是"抖 / 糊 / 呲"。

这一版给每个候选同时算三类数字：

    A. 残留（explained）—— 把人声轨逐帧投影到伴奏轨上，能被"伴奏的某个
       复数倍"解释掉的那部分能量就是漏进来的伴奏。比只看绝对能量可靠：
       它天然排除了"人声本来就响"的影响，而且复数投影算进了相位。
       分五个频段报，能直接看出漏的是贝斯还是镲。

    B. 损伤（vocal_loss）—— 只在**人声占优**的时频点（|V| > |A|，且落在
       150~5000 Hz 人声的主体频段）上统计处理前后能量差。这些点按定义
       几乎不含伴奏，在那里掉能量就是实打实的人声损伤。

    C. 抖动（pump）—— 逐帧看实际施加的增益 g(t)=|V'|/|V|，取其在有人声的
       帧上的标准差。时变掩蔽的典型 artifact 就是"忽大忽小"，这个数字
       直接把它量出来。> 2 dB 就很难听了。

三类中 A 越低越好、B 绝对值越小越好、C 越小越好。只有 A 明显降而
B、C 几乎不动的方案才算真的"改善了"。
"""
import argparse
import os
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.join(ROOT, "host")
OUT = os.path.join(ROOT, "tool", "logs", "sep_eval")
DEFAULT_SRC = r"C:/Users/Administrator/Downloads/老狼&王婧_想把我唱给你听.flac"

for _p in (HOST, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import soundfile as sf  # noqa: E402


# ---------------------------------------------------------------- 基础 DSP

def stft(x, n_fft=2048, hop=512):
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    pad = n_fft // 2
    xp = np.concatenate([np.zeros(pad), x, np.zeros(pad)])
    nf = 1 + (len(xp) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(nf)[:, None]
    w = np.hanning(n_fft)
    return np.fft.rfft(xp[idx] * w, axis=1)


def istft(S, n_out, n_fft=2048, hop=512):
    w = np.hanning(n_fft)
    fr = np.fft.irfft(S, n=n_fft, axis=1) * w
    total = hop * (fr.shape[0] - 1) + n_fft
    acc = np.zeros(total)
    wsum = np.zeros(total)
    for i in range(fr.shape[0]):
        s = i * hop
        acc[s:s + n_fft] += fr[i]
        wsum[s:s + n_fft] += w ** 2
    acc /= np.maximum(wsum, 1e-8)
    pad = n_fft // 2
    y = acc[pad:pad + n_out]
    if len(y) < n_out:
        y = np.concatenate([y, np.zeros(n_out - len(y))])
    return y


def freqs(sr, n_fft=2048):
    return np.fft.rfftfreq(n_fft, 1.0 / sr)


def band_sel(f, lo, hi):
    return (f >= lo) & (f < hi)


# ---------------------------------------------------------------- A. 残留
#
# 逐帧做复数最小二乘：把 V_f 投影到 A_f 上，
#       c = <A, V> / <A, A>,  残差 = V - c·A
# 能被解释的比例 r = 1 - ||残差||² / ||V||²。
# ★ 复数投影是关键：只用幅度做比会把相位不同的成分算成"没解释"，
#   而</>漏进人声轨的伴奏和伴奏轨本身是同相的（都来自同一条源）。

def explained(V, A, sr, n_fft=2048, hop=512, bands=None):
    """返回 {band: (能量加权解释率 dB, 前 10% 最差帧的 dB)}。"""
    if bands is None:
        bands = [("LF", 0, 200), ("LM", 200, 800), ("MID", 800, 3000),
                 ("HI", 3000, 8000), ("HH", 8000, 16000)]
    f = freqs(sr, n_fft)
    SV, SA = stft(V, n_fft, hop), stft(A, n_fft, hop)
    n = min(len(SV), len(SA))
    SV, SA = SV[:n], SA[:n]
    out = {}
    for name, lo, hi in bands:
        sel = band_sel(f, lo, hi)
        if not sel.any():
            out[name] = (float("nan"), float("nan"))
            continue
        v, a = SV[:, sel], SA[:, sel]
        den = np.sum(np.abs(a) ** 2, axis=1) + 1e-30
        c = np.sum(np.conj(a) * v, axis=1) / den
        res = v - c[:, None] * a
        ev = np.sum(np.abs(v) ** 2, axis=1)
        er = np.sum(np.abs(res) ** 2, axis=1)
        r = 1.0 - er / np.maximum(ev, 1e-30)
        r = np.clip(r, 1e-12, 1.0)
        wmean = float(np.sum(ev * r) / np.sum(ev))
        # 只看有能量的帧，取最差的一成 —— 用户听到的"某一瞬间有镲"在这里
        thr = np.percentile(ev, 50)
        pick = ev > thr
        if pick.sum() < 8:
            pick = np.ones_like(ev, dtype=bool)
        p90 = float(np.percentile(r[pick], 90))
        out[name] = (10 * np.log10(max(wmean, 1e-12)),
                     10 * np.log10(max(p90, 1e-12)))
    return out


def band_rel(V, sr, lo, hi, ref_lo, ref_hi, n_fft=4096, hop=1024):
    """V 在 [lo,hi) 的能量相对 [ref_lo,ref_hi) 的 dB。看绝对残留分布。"""
    f = freqs(sr, n_fft)
    S = stft(np.asarray(V, dtype=np.float64), n_fft, hop)
    P = np.abs(S) ** 2
    a = P[:, band_sel(f, lo, hi)].mean()
    b = P[:, band_sel(f, ref_lo, ref_hi)].mean()
    return 10 * np.log10(max(a, 1e-30) / max(b, 1e-30))


# ---------------------------------------------------------------- B/C. 损伤与抖动

def damage(V0, V1, A, sr, n_fft=2048, hop=512):
    """V0=处理前, V1=处理后。

    voc_loss   : 150~5000 Hz 人声占优时频点的能量变化（负 = 人声本体被削）
    voc_loss_hi: 4000~9000 Hz 同上（负 = 齿音/空气感被吃掉）
    pump       : 有人声的帧上增益的标准差（dB），时变掩蔽的 artifact
    """
    f = freqs(sr, n_fft)
    S0, S1, SA = (stft(np.asarray(x, dtype=np.float64), n_fft, hop)
                  for x in (V0, V1, A))
    n = min(len(S0), len(S1), len(SA))
    S0, S1, SA = S0[:n], S1[:n], SA[:n]
    v0, v1, a = S0, S1, SA
    # 人声占优点：|V| 明显大于同帧同频的 |A|
    dom = np.abs(v0) > (np.abs(a) * 2.0)
    if dom.sum() < 64:
        return float("nan"), float("nan"), float("nan")
    msel = band_sel(f, 150, 5000)
    hsel = band_sel(f, 4000, 9000)
    out = []
    for ssel in (msel, hsel):
        dm = dom & ssel[None, :]
        out.append(10 * np.log10(np.sum(np.abs(v1[dm]) ** 2) /
                                 max(np.sum(np.abs(v0[dm]) ** 2), 1e-30))
                   if dm.sum() >= 16 else float("nan"))
    # 抖动：每帧的实际增益
    e0 = np.sum(np.abs(v0) ** 2, axis=1)
    e1 = np.sum(np.abs(v1) ** 2, axis=1)
    live = e0 > np.percentile(e0, 25)
    g = 10 * np.log10(np.sqrt(np.maximum(e1, 1e-30) /
                              np.maximum(e0, 1e-30)))
    pump = float(np.std(g[live])) if live.sum() > 8 else float("nan")
    return out[0], out[1], pump


# ---------------------------------------------------------------- 评测入口

def evaluate(tag, V, A, sr, V0=None):
    V = np.asarray(V, dtype=np.float64)
    A = np.asarray(A, dtype=np.float64)
    n = min(len(V), len(A))
    V, A = V[:n], A[:n]
    vm = V.mean(1) if V.ndim == 2 else V
    am = A.mean(1) if A.ndim == 2 else A
    exp = explained(vm, am, sr)
    row = dict(tag=tag)
    for k, (wm, p90) in exp.items():
        row[k + "_w"] = wm
        row[k + "_p90"] = p90
    row["bass_rel"] = band_rel(vm, sr, 30, 110, 300, 3400)
    row["cymb_rel"] = band_rel(vm, sr, 7000, 14000, 300, 3400)
    if V0 is not None:
        v0m = np.asarray(V0, dtype=np.float64)
        v0m = v0m.mean(1) if v0m.ndim == 2 else v0m
        row["voc_loss"], row["voc_loss_hi"], row["pump"] = damage(
            v0m, vm, am, sr)
    else:
        row["voc_loss"] = row["voc_loss_hi"] = row["pump"] = 0.0
    return row


def fmt(rows):
    cols = ["tag", "MID_p90", "HI_p90", "HH_p90",
            "bass_rel", "cymb_rel", "voc_loss", "voc_loss_hi", "pump"]
    w = [max(10, len(c)) for c in cols]
    print(" | ".join(c.rjust(w[i]) for i, c in enumerate(cols)))
    print("-+-".join("-" * x for x in w))
    for r in rows:
        cells = []
        for i, c in enumerate(cols):
            v = r.get(c)
            if isinstance(v, str):
                s = v
            elif v is None or (isinstance(v, float) and np.isnan(v)):
                s = ""
            else:
                s = "%.2f" % v
            cells.append(s.rjust(w[i]))
        print(" | ".join(cells))
    print()
    print("读法：*_p90 = 该频段最差一成帧里人声轨可被伴奏解释的比例(dB)，越负越干净；")
    print("      bass_rel/cymb_rel = 低频/高频绝对含量相对人声中频，越负越干净；")
    print("      voc_loss = 人声占优点的能量变化(负=损伤)；pump = 逐帧增益抖动 dB。")


# ---------------------------------------------------------------- 候选后处理

def mask_stereo(V, m):
    """给立体声逐声道乘同一个 mask（保住左右相位关系）。"""
    V = np.asarray(V, dtype=np.float64)
    one = (V.ndim == 1)
    if one:
        V = V.reshape(-1, 1)
    S = stft(V[:, 0])
    S = S[:len(m)] * m
    y0 = istft(S, V.shape[0])
    S = stft(V[:, 1])
    S = S[:len(m)] * m
    y1 = istft(S, V.shape[0])
    out = np.stack([y0, y1], axis=1).astype(np.float32)
    return out[:, 0] if one else out


def wiener(V, Ref, alpha=1.0, floor=0.25, kt=3, kf=5):
    """用 Ref 做参考的全频带 Wiener 掩蔽（现有做法，作为对照）。"""
    vm = np.asarray(V, dtype=np.float64)
    vm = vm.mean(1) if vm.ndim == 2 else vm
    rm = np.asarray(Ref, dtype=np.float64)
    rm = rm.mean(1) if rm.ndim == 2 else rm
    MV, MR = stft(vm), stft(rm)
    n = min(len(MV), len(MR))
    mv, mr = np.abs(MV[:n]), np.abs(MR[:n])
    mr = _box2d(mr, kt, kf)
    m = mv ** 2 / (mv ** 2 + alpha * mr ** 2 + 1e-30)
    return np.maximum(m, floor)


def _box(a, k, axis):
    if k <= 1:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (k // 2, k - 1 - k // 2)
    p = np.pad(a, pad, mode="edge")
    c = np.cumsum(p, axis=axis)
    z = np.zeros_like(np.take(c, [0], axis=axis))
    c = np.concatenate([z, c], axis=axis)
    nn = c.shape[axis]
    hi = np.take(c, np.arange(k, nn), axis=axis)
    lo = np.take(c, np.arange(0, nn - k), axis=axis)
    return (hi - lo) / float(k)


def _box2d(A, kt, kf):
    return _box(_box(A, kt, 0), kf, 1)


def wiener_band(V, Ref, sr, fband, alpha=2.0, floor=0.05, kt=3, kf=5,
                gate_db=6.0):
    """**只作用在指定频段**的 Wiener 掩蔽 + 人声占优时松开。

    和 wiener() 的区别有三条，每一条都是为了少伤到人声：
      1. 频段外 mask 恒为 1 —— 镲的问题不动中频，贝斯的问题不动高次谐波。
      2. gate：V 比 Ref 还强的时间点（明显是人声在唱）直接不压。
      3. 允许更低的 floor（频段窄，泵感不明显），换来更狠的残留压制。
    """
    vm = np.asarray(V, dtype=np.float64)
    vm = vm.mean(1) if vm.ndim == 2 else vm
    rm = np.asarray(Ref, dtype=np.float64)
    rm = rm.mean(1) if rm.ndim == 2 else rm
    MV, MR = stft(vm), stft(rm)
    n = min(len(MV), len(MR))
    MV, MR = MV[:n], MR[:n]
    mv, mr = np.abs(MV), np.abs(MR)
    f = freqs(sr)
    sel = band_sel(f, fband[0], fband[1])
    mr_s = _box2d(mr, kt, kf)
    m = mv ** 2 / (mv ** 2 + alpha * mr_s ** 2 + 1e-30)
    m = np.maximum(m, floor)
    # gate：整帧能量 V 明显大于 Ref 时不压
    ev = np.sum(np.abs(MV) ** 2, axis=1)
    er = np.sum(np.abs(MR) ** 2, axis=1)
    keep = 10 * np.log10(ev / np.maximum(er, 1e-30)) > gate_db
    m[keep, :] = 1.0
    full = np.ones_like(m)
    full[:, sel] = m[:, sel]
    return full


def ms_side(V, k=0.5):
    """把人声轨的 side 分量按 k 衰减（人声居中、漏进来的乐器常被声场摊宽）。"""
    V = np.asarray(V, dtype=np.float64)
    if V.ndim == 1:
        return V
    mid = 0.5 * (V[:, 0] + V[:, 1])
    side = 0.5 * (V[:, 0] - V[:, 1])
    return np.stack([mid + k * side, mid - k * side], axis=1)


# ---------------------------------------------------------------- 主流程

PRESETS = {
    "std": dict(model="htdemucs", shifts=2, overlap=0.50),
    "ft": dict(model="htdemucs_ft", shifts=2, overlap=0.50),
    "mdx": dict(model="mdx_extra", shifts=0, overlap=0.50),
    "ht0": dict(model="htdemucs", shifts=0, overlap=0.25),
}


def cmd_ctl(a):
    """指标自检：证明这些数字真的在量"漏"和"伤"，而不是恒真的装饰。

    负对照   把伴奏轨整体循环移位 13 s 再投影。它的频谱特性没变，
             但和時間对不上了；若 explained 仍然很差，说明这个指标量的是
             "频谱相似"而不是"残留"。
    损伤正对照 故意加 4 dB 随机逐帧抖动 + −2 dB 全频静态衰减。
             pump 应该报 ~4 dB，voc_loss 应该报 ~−2 dB。报不出来就是指标坏。
    """
    d = os.path.join(OUT, a.tag)
    V, sr = sf.read(os.path.join(d, "vocals.wav"), dtype="float32",
                    always_2d=True)
    A, _ = sf.read(os.path.join(d, "acc.wav"), dtype="float32",
                   always_2d=True)
    vm, am = V.mean(1), A.mean(1)
    rows = [evaluate("%s/raw" % a.tag, V, A, sr, None)]

    shift = int(13 * sr)
    Ar = np.roll(A, shift, axis=0)
    rows.append(evaluate("%s/负对照-移位" % a.tag, V, Ar, sr, None))

    # 故意制造 pump
    f = freqs(sr)
    S = stft(vm)
    rng = np.random.default_rng(0)
    g = 10 ** (rng.normal(0.0, 0.2, size=(len(S), 1)) / 20.0)
    Vd = istft(S * g, len(vm))
    rows.append(evaluate("%s/正对照-抖动" % a.tag, Vd, A, sr, vm))
    Vq = (vm * 0.794).astype(np.float32)   # -2 dB
    rows.append(evaluate("%s/正对照-衰减" % a.tag, Vq, A, sr, vm))
    fmt(rows)


def cmd_run(a):
    """跑一次 demucs 并把原始 stems 存下来。"""
    sys.path.insert(0, HOST)
    import local_ai
    x, sr = sf.read(a.src, dtype="float32", always_2d=True)
    i0 = int(a.start * sr)
    i1 = min(len(x), i0 + int(a.secs * sr))
    x = x[i0:i1]
    pre = PRESETS[a.preset]
    # 直接用 separate 的内部参数（不想为了评测再改一遍 QUALITY_PRESETS）
    import os as _os
    _os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    voc, acc, drm, info = _sep_raw(x, sr, pre, a.device)
    d = os.path.join(OUT, a.tag)
    _os.makedirs(d, exist_ok=True)
    for nm, v in (("vocals", voc), ("acc", acc), ("drums", drm), ("mix", x)):
        sf.write(os.path.join(d, nm + ".wav"), v, sr)
    print("saved ->", d, info)


def _sep_raw(x, sr, pre, dev="cuda"):
    """绕过 QUALITY_PRESETS，直接按给定 model/shifts/overlap 跑 demucs。"""
    import torch
    from demucs.apply import apply_model
    sys.path.insert(0, HOST)
    import local_ai
    local_ai.ensure_model_endpoint()
    mdl = local_ai._get_model(pre["model"], dev)
    msr = int(getattr(mdl, "samplerate", 44100))
    seg = local_ai._model_segment(mdl)
    wav = torch.from_numpy(np.ascontiguousarray(
        np.asarray(x, dtype=np.float32).T)).float()
    ref = wav.mean(0)
    std = ref.std() + 1e-8
    wn = (wav - ref.mean()) / std
    with torch.no_grad():
        out = apply_model(mdl, wn[None], shifts=pre["shifts"], split=True,
                          overlap=pre["overlap"], progress=False,
                          device=dev, segment=seg)[0]
    out = out.float().cpu().numpy() * float(std) + float(ref.mean())
    names = list(getattr(mdl, "sources", ["drums", "bass", "other", "vocals"]))
    vi = names.index("vocals") if "vocals" in names else len(names) - 1
    di = names.index("drums") if "drums" in names else -1
    voc = out[vi].T.astype(np.float32)
    acc = (out.sum(0) - out[vi]).T.astype(np.float32)
    drm = out[di].T.astype(np.float32) if di >= 0 else np.zeros_like(voc)
    n = len(x)
    voc, acc, drm = voc[:n], acc[:n], drm[:n]
    return voc, acc, drm, dict(model=pre["model"], shifts=pre["shifts"],
                              overlap=pre["overlap"], seg=seg, sr=msr)


def cmd_eval(a):
    """对已存的一组 stems 跑候选后处理并打分。"""
    rows = []
    HI = (a.lo, a.hi)
    plan = [("raw", lambda V, A, D: V)]
    if a.full:
        plan.append(("full_a%.1f" % a.alpha,
                     lambda V, A, D: mask_stereo(
                         V, wiener(V, A, alpha=a.alpha, floor=0.25))))
    if a.wdrum:
        plan.append(("drumFull", lambda V, A, D: mask_stereo(
            V, wiener(V, D, alpha=a.alpha, floor=a.floor))))
    if a.hiacc:
        plan.append(("hiA_a%.1f" % a.alpha,
                     lambda V, A, D: mask_stereo(V, wiener_band(
                         V, A, sr, HI, alpha=a.alpha, floor=a.floor,
                         gate_db=a.gate))))
    if a.hidrum:
        plan.append(("hiD_a%.1f" % a.alpha,
                     lambda V, A, D: mask_stereo(V, wiener_band(
                         V, D, sr, HI, alpha=a.alpha, floor=a.floor,
                         gate_db=a.gate))))
    for tag in a.tags:
        d = os.path.join(OUT, tag)
        V, sr = sf.read(os.path.join(d, "vocals.wav"), dtype="float32",
                        always_2d=True)
        A, _ = sf.read(os.path.join(d, "acc.wav"), dtype="float32",
                       always_2d=True)
        D, _ = sf.read(os.path.join(d, "drums.wav"), dtype="float32",
                       always_2d=True)
        od = os.path.join(d, "post")
        os.makedirs(od, exist_ok=True)
        for vn, fn in plan:
            VV = V if vn == "raw" else fn(V, A, D)
            rows.append(evaluate(tag + "/" + vn, VV, A, sr,
                                 None if vn == "raw" else V))
            sf.write(os.path.join(od, vn + ".wav"),
                     np.asarray(VV, dtype=np.float32), sr)
    fmt(rows)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers()
    r = sub.add_parser("run")
    r.add_argument("--src", default=DEFAULT_SRC)
    r.add_argument("--start", type=float, default=80.0)
    r.add_argument("--secs", type=float, default=30.0)
    r.add_argument("--tag", required=True)
    r.add_argument("--preset", default="std")
    r.add_argument("--device", default="cuda")
    r.set_defaults(fn=cmd_run)

    c1 = sub.add_parser("ctl")
    c1.add_argument("--tag", required=True)
    c1.set_defaults(fn=cmd_ctl)

    e = sub.add_parser("eval")
    e.add_argument("--tags", nargs="+", required=True)
    e.add_argument("--full", action="store_true", help="全频带 Wiener（现役做法）")
    e.add_argument("--wdrum", action="store_true", help="全频带 Wiener + drums 参考")
    e.add_argument("--hiacc", action="store_true", help="限高频 + 伴奏参考")
    e.add_argument("--hidrum", action="store_true", help="限高频 + drums 参考")
    e.add_argument("--lo", type=float, default=4500.0)
    e.add_argument("--hi", type=float, default=16000.0)
    e.add_argument("--gate", type=float, default=6.0)
    e.add_argument("--alpha", type=float, default=2.0)
    e.add_argument("--floor", type=float, default=0.05)
    e.set_defaults(fn=cmd_eval)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
