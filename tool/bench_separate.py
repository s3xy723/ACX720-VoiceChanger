# -*- coding: utf-8 -*-
"""bench_separate.py —— 分离效果的客观对比台。

为什么要有它
------------
"分离不干净"这句话本身不可测。真正能量化的是两件事：

  1. bleed（人声轨里的伴奏残留）
     在**本来没有人声**的段落（前奏/间奏），人声轨还剩多少能量。
     指标：bleed_db = 20log10(rms(voc) / rms(mix))  —— 越负越好，-30 dB 以下基本听不见。
  2. swell（伴奏轨随人声起伏）
     人声段和伴奏段，伴奏轨的电平差多少。
     指标：swell_db = 20log10(rms(acc[人声段]) / rms(acc[伴奏段])) —— 理想 ≈ 0。
     明显为正说明人声漏进伴奏了。

段落怎么划分（没有 ground truth 时的唯一办法）
-----------------------------------------------
先跑两个**不同架构**的模型，取逐帧活动度 env = min(env_a, env_b)。
"两个模型都认为这里没人声"才判为伴奏段 —— 单模型自己判会自证其罪。
再用混音能量做门限：mix 明显有声音、env 最低的那批帧 = 伴奏段。

用法
----
    python tool/bench_separate.py run --model htdemucs --tag base
    python tool/bench_separate.py cmp --tags base mdx_extra ft
    python tool/bench_separate.py table --tag base      # 逐半秒 dump
"""
import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

BENCH = os.path.join(ROOT, "tool", "logs", "sep_bench")
# 没有默认素材：评测必须显式给 --src（原版写死了某台机器上的下载路径，
# 别人克隆下来直接跑会 FileNotFoundError）。
DEFAULT_SRC = None


def load_src(src, secs, start):
    import soundfile as sf
    info = sf.info(src)
    f0 = int(max(0.0, start) * info.samplerate)
    n = int(secs * info.samplerate) if secs else -1
    x, sr = sf.read(src, start=f0, frames=n, dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return x[:, :2].copy(), int(sr)


def rms_env(x, sr, win=0.5):
    """逐帧 RMS（单声道折叠）。返回 (帧中心时间, dBFS)。"""
    m = x.mean(axis=1) if x.ndim == 2 else x
    n = max(1, int(round(win * sr)))
    k = len(m) // n
    if k == 0:
        return np.array([0.0]), np.array([-120.0])
    e = m[:k * n].reshape(k, n)
    r = np.sqrt(np.mean(e.astype(np.float64) ** 2, axis=1) + 1e-30)
    t = (np.arange(k) + 0.5) * win
    return t, 20 * np.log10(r + 1e-12)


def pick_segments(t, mix_db, act, floor_pct=25, mix_gate=12.0):
    """返回 (伴奏段掩码, 人声段掩码)。

    act: 多模型交叉得到的人声活动度（dB）。
    门限思路：
      伴奏段 = mix 不低于最大值 12 dB（排除静音） 且 act 落在最低 floor_pct%
      人声段 = act 落在最高 25%（这才是"两个模型都听到人声"的地方）
    """
    voiced = mix_db > (mix_db.max() - mix_gate)
    # 活动度归一：相对它自己的高分位
    hi = np.percentile(act, 95)
    rel = act - hi
    acc_m = voiced & (rel < np.percentile(rel[voiced], floor_pct))
    voc_m = voiced & (rel > np.percentile(rel[voiced], 75))
    return acc_m, voc_m


def metrics(voc, acc, mix, sr, acc_m, voc_m):
    out = {}
    for name, m in (("acc", acc_m), ("voc", voc_m)):
        pass
    def band_db(sig, m, win=0.5):
        t, db = rms_env(sig, sr, win)
        n = min(len(db), len(m))
        return db[:n], m[:n]

    dv, _ = band_db(voc, acc_m)
    dm, _ = band_db(mix, acc_m)
    da, _ = band_db(acc, acc_m)
    n = min(len(dv), len(dm), len(da), len(acc_m))
    am = acc_m[:n]
    out["bleed_db"] = float(np.mean(dv[:n][am] - dm[:n][am])) if am.any() else float("nan")

    dv2, _ = band_db(voc, voc_m)
    da2, _ = band_db(acc, voc_m)
    n2 = min(len(dv2), len(da2), len(voc_m))
    vm = voc_m[:n2]
    if vm.any() and am.any():
        # 伴奏轨在人声段 / 伴奏段 的电平差
        out["swell_db"] = float(np.mean(da2[:n2][vm]) - np.mean(da[:n][am]))
        # 人声轨在人声段 / 混音 的比例（人声占多少）
        out["vocal_share_db"] = float(np.mean(dv2[:n2][vm] - band_db(mix, voc_m)[0][:n2][vm]))
    else:
        out["swell_db"] = out["vocal_share_db"] = float("nan")

    # 低频泄漏：人声轨 30~70 Hz 相对 200~4000 Hz（鼓/贝斯的高频泛音不算）
    out["low_leak_db"] = _band_ratio(voc, sr, 30, 70, 200, 4000)
    # 人声轨的"冲击性"：短时能量峰均比，鼓点会拉高
    out["crest_db"] = _crest(voc, sr)
    return out


def _spec(x, sr):
    m = x.mean(axis=1) if x.ndim == 2 else x
    w = np.hanning(len(m))
    X = np.abs(np.fft.rfft(m * w)) ** 2 + 1e-20
    f = np.fft.rfftfreq(len(m), 1.0 / sr)
    return f, X


def _band_ratio(x, sr, lo1, hi1, lo2, hi2):
    f, X = _spec(x, sr)
    a = X[(f >= lo1) & (f < hi1)].sum() + 1e-20
    b = X[(f >= lo2) & (f < hi2)].sum() + 1e-20
    return float(10 * np.log10(a / b))


def _crest(x, sr, win=0.02):
    m = x.mean(axis=1) if x.ndim == 2 else x
    n = max(1, int(win * sr))
    k = len(m) // n
    if k < 2:
        return float("nan")
    r = np.sqrt(np.mean(m[:k * n].reshape(k, n).astype(np.float64) ** 2, axis=1) + 1e-20)
    return float(20 * np.log10(r.max() / (np.median(r) + 1e-20)))


def cmd_run(a):
    os.makedirs(os.path.join(BENCH, a.tag), exist_ok=True)
    import soundfile as sf
    x, sr = load_src(a.src, a.secs, a.start)
    print("素材 %s  %.1f s  %d Hz  %d ch" % (os.path.basename(a.src), len(x) / sr, sr, x.shape[1]))

    import local_ai as la
    if a.model:
        la.DEMUCS_MODEL = a.model
        la._model = None
        la._model_name = None
    t0 = time.time()
    voc, acc, info = la.separate(x, sr, device=a.device, quality=a.quality,
                                progress=lambda s: print("  " + s))
    dt = time.time() - t0
    d = os.path.join(BENCH, a.tag)
    sf.write(os.path.join(d, "mix.wav"), x, sr)
    sf.write(os.path.join(d, "vocals.wav"), voc, sr)
    sf.write(os.path.join(d, "acc.wav"), acc, sr)
    meta = dict(tag=a.tag, model=info["model"], quality=a.quality, sr=sr,
                secs=len(x) / sr, cost_s=round(dt, 2),
                rtf=round(dt / (len(x) / sr), 3), info=info)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print("完成 %s  %.1f s  (RTF %.2f)" % (a.tag, dt, meta["rtf"]))
    print(json.dumps({k: info[k] for k in ("device", "model", "model_sr",
                                          "shifts", "overlap", "segment")},
                     ensure_ascii=False))


def _load_tag(tag):
    import soundfile as sf
    d = os.path.join(BENCH, tag)
    if not os.path.isdir(d):
        raise SystemExit("没有这个 tag: %s" % tag)
    mix, sr = sf.read(os.path.join(d, "mix.wav"), dtype="float32", always_2d=True)
    voc, _ = sf.read(os.path.join(d, "vocals.wav"), dtype="float32", always_2d=True)
    acc, _ = sf.read(os.path.join(d, "acc.wav"), dtype="float32", always_2d=True)
    return mix, voc, acc, sr


def cmd_cmp(a):
    rows = []
    acts = {}
    for tag in a.tags:
        mix, voc, acc, sr = _load_tag(tag)
        t, _ = rms_env(voc, sr, 0.5)
        acts[tag] = rms_env(voc, sr, 0.5)[1]
    # 交叉 VAD：所有 tag 的活动度取逐帧最小值（都对不上才判"没人声"）
    n = min(len(v) for v in acts.values())
    cross = np.min(np.vstack([v[:n] for v in acts.values()]), axis=0)

    for tag in a.tags:
        mix, voc, acc, sr = _load_tag(tag)
        t, mix_db = rms_env(mix, sr, 0.5)
        m = min(len(mix_db), len(cross))
        acc_m, voc_m = pick_segments(t[:m], mix_db[:m], cross[:m],
                                     floor_pct=a.floor, mix_gate=a.gate)
        met = metrics(voc, acc, mix, sr, acc_m, voc_m)
        met["tag"] = tag
        met["acc_seg_s"] = float(acc_m.sum() * 0.5)
        met["voc_seg_s"] = float(voc_m.sum() * 0.5)
        rows.append(met)

    hdr = ("%-16s %10s %10s %12s %12s %8s" %
           ("tag", "bleed_dB", "swell_dB", "vocal_share", "low_leak", "crest"))
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print("%-16s %10.1f %10.1f %12.1f %12.1f %8.1f" %
              (r["tag"], r["bleed_db"], r["swell_db"], r["vocal_share_db"],
               r["low_leak_db"], r["crest_db"]))
    print("\n判读：bleed 越负越好（<-30 基本听不见）；swell 越接近 0 越好；"
          "vocal_share 越接近 0 说明人声轨拿到的真的是人声")
    print("段长：伴奏段 %.1f s，人声段 %.1f s" % (rows[0]["acc_seg_s"], rows[0]["voc_seg_s"]))


def cmd_table(a):
    mix, voc, acc, sr = _load_tag(a.tag)
    t, db = rms_env(mix, sr, 0.5)
    _, dv = rms_env(voc, sr, 0.5)
    _, da = rms_env(acc, sr, 0.5)
    print("%7s %9s %9s %9s %9s" % ("t(s)", "mix", "voc", "acc", "voc-mix"))
    for i in range(min(len(db), len(dv), len(da))):
        print("%7.1f %9.1f %9.1f %9.1f %9.1f" % (t[i], db[i], dv[i], da[i], dv[i] - db[i]))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--src", default=DEFAULT_SRC, required=(DEFAULT_SRC is None))
    r.add_argument("--secs", type=float, default=45.0)
    r.add_argument("--start", type=float, default=0.0)
    r.add_argument("--tag", required=True)
    r.add_argument("--model", default="")
    r.add_argument("--quality", default="std")
    r.add_argument("--device", default="auto")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("cmp")
    c.add_argument("--tags", nargs="+", required=True)
    c.add_argument("--floor", type=int, default=25)
    c.add_argument("--gate", type=float, default=12.0)
    c.set_defaults(fn=cmd_cmp)

    tb = sub.add_parser("table")
    tb.add_argument("--tag", required=True)
    tb.set_defaults(fn=cmd_table)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
