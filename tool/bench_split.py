# -*- coding: utf-8 -*-
"""
bench_split.py —— 男女声分离的有真值基准

自己合成一段"二重唱"：男声 / 女声 / 伴奏三条已知音轨，混成一首立体声"歌"。
然后拿分离结果和真值逐条比 SI-SDR，这样改算法才有客观依据，
而不是靠"听起来好像好一点"。

合成的内容尽量贴近真实歌曲的结构：
    0- 8 s   前奏（只有伴奏）
    8-24 s   男声独唱
   24-40 s   女声独唱
   40-56 s   男女齐唱（这段是真正的难点）
   56-64 s   间奏
   64-72 s   男女齐唱

用法：
    python tool/bench_split.py             # 跑当前 vocal_split 的实现
    python tool/bench_split.py --save out.wav
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "host")))

SR = 44100


# ---------------------------------------------------------------- 合成素材

def _harm_voice(n, f0_curve, formants, sr=SR, seed=0, breath=0.012, phase0=0.0):
    """按基频曲线合成一个"人声"：谐波序列 × 共振峰包络 + 一点气声。"""
    rng = np.random.RandomState(seed)
    t = np.arange(n) / float(sr)
    # 相位由基频积分得到，保证连续
    ph = phase0 + 2.0 * np.pi * np.cumsum(f0_curve) / sr
    x = np.zeros(n)
    for h in range(1, 41):
        f = h * f0_curve
        # 共振峰包络（每帧算一次太慢，用均值近似即可）
        env = 0.0
        for (fc, bw, g) in formants:
            env += g / (1.0 + ((f.mean() - fc) / bw) ** 2)
        a = env / (h ** 0.9)
        if f.mean() > sr * 0.45:
            break
        if a < 1e-4:
            continue
        x += a * np.sin(h * ph)
    x /= np.abs(x).max() + 1e-9
    if breath:
        nb = rng.randn(n)
        # 简单高通，让噪声更像气声而不是低频隆隆
        nb = np.concatenate([[0.0], np.diff(nb)])
        x = x + breath * nb / (np.abs(nb).max() + 1e-9)
    return x.astype(np.float64)


def _f0_curve(n, base, vib_hz=5.2, vib_cents=25.0, drift=0.03, seed=0, notes=None,
              sr=SR):
    rng = np.random.RandomState(seed)
    t = np.arange(n) / float(sr)
    f = np.full(n, float(base))
    if notes is not None:
        # 旋律：按 (起始秒, 半音偏移, 时长秒) 走
        f = np.full(n, base)
        for (t0, semi, dur) in notes:
            a = int(t0 * sr)
            b = min(n, int((t0 + dur) * sr))
            if a >= n:
                break
            f[a:b] = base * 2.0 ** (semi / 12.0)
    # 颤音 + 缓慢漂移
    f = f * (1.0 + drift * 0.5 * np.sin(2 * np.pi * 0.17 * t + seed))
    f = f * 2.0 ** ((vib_cents / 1200.0) * np.sin(2 * np.pi * vib_hz * t))
    return f


def _accompaniment(n, sr=SR, seed=7):
    """伴奏：几个和弦音 + 贝斯 + 一点点节奏噪声，整体铺得比较宽。"""
    rng = np.random.RandomState(seed)
    t = np.arange(n) / float(sr)
    L = np.zeros(n)
    R = np.zeros(n)
    # 和弦走向（C-Am-F-G），每 4 秒一换
    roots = [0, -3, -7, -5]
    for i in range(0, n, 4 * sr):
        e = min(n, i + 4 * sr)
        seg = t[i:e]
        k = (i // (4 * sr)) % 4
        base = 130.81 * 2.0 ** (roots[k] / 12.0)
        for j, semi in enumerate((0, 4, 7, 12)):
            fq = base * 2.0 ** (semi / 12.0) * 2.0
            amp = 0.16 / (1 + 0.3 * j)
            pan = -0.6 + 0.4 * j
            w = np.sin(2 * np.pi * fq * seg + j)
            L[i:e] += amp * (1 - max(0.0, pan)) * w
            R[i:e] += amp * (1 + min(0.0, pan)) * w
        # 贝斯：男声音域以下的低音，居中
        fb = base / 2.0
        bass = 0.30 * np.sin(2 * np.pi * fb * seg)
        L[i:e] += bass
        R[i:e] += bass
    # 节奏：滤波噪声当鼓
    hits = np.zeros(n)
    for s in range(0, n, sr // 2):
        hit = rng.randn(min(sr // 8, n - s))
        hit = np.convolve(hit, np.exp(-np.arange(60) / 12.0), mode="same")
        hits[s:s + hit.size] += hit * 0.5
    hits = hits - np.concatenate([[0.0], hits[:-1]]) * 0.6      # 高通一点
    L += 0.10 * hits
    R += 0.10 * np.roll(hits, 5)                                # 鼓略错开，做宽
    m = max(np.abs(L).max(), np.abs(R).max())
    return (L / m).astype(np.float64), (R / m).astype(np.float64)


def make_duet(sr=SR):
    """返回 (混音立体声, 真值 dict)。"""
    dur = 72.0
    n = int(dur * sr)
    vsrc_n = int(32 * sr)          # 男/女各准备 32 秒素材（唱段拼起来要用到）

    # 男声：偏低的中音，110-155 Hz
    male_notes = [(0, 0, 2), (2, 2, 2), (4, -2, 2), (6, 3, 2), (8, 0, 2),
                  (10, 5, 2), (12, -3, 2), (14, 2, 2)]
    fm = _f0_curve(vsrc_n, 118.0, seed=1, notes=male_notes, sr=sr)
    male = _harm_voice(vsrc_n, fm, [(600, 90, 1.0), (1100, 130, 0.55),
                                    (2600, 200, 0.22)], sr=sr, seed=3)
    # 女声：200-300 Hz
    fem_notes = [(0, 0, 2), (2, -3, 2), (4, 2, 2), (6, 4, 2), (8, 0, 2),
                 (10, 2, 2), (12, -1, 2), (14, 5, 2)]
    ff = _f0_curve(vsrc_n, 232.0, seed=2, notes=fem_notes, sr=sr)
    female = _harm_voice(vsrc_n, ff, [(700, 110, 1.0), (1300, 160, 0.6),
                                      (3000, 260, 0.28)], sr=sr, seed=4)

    M = np.zeros(n); F = np.zeros(n)
    # 8-24 男独唱；24-40 女独唱；40-56 齐唱；64-72 齐唱
    M[8 * sr:24 * sr] = male[0:16 * sr]
    F[24 * sr:40 * sr] = female[0:16 * sr]
    M[40 * sr:56 * sr] = male[16 * sr:32 * sr]
    F[40 * sr:56 * sr] = female[16 * sr:32 * sr]
    M[64 * sr:72 * sr] = male[24 * sr:32 * sr]
    F[64 * sr:72 * sr] = female[24 * sr:32 * sr]

    accL, accR = _accompaniment(n, sr=sr)
    # 人声居中，伴奏立体声
    L = 0.55 * (M + F) + 0.9 * accL
    R = 0.55 * (M + F) + 0.9 * accR
    mix = np.stack([L, R], axis=1).astype(np.float32)
    peak = np.abs(mix).max()
    if peak > 0.99:
        mix = (mix / peak * 0.98).astype(np.float32)
    gt = dict(male=M.astype(np.float32), female=F.astype(np.float32),
              acc_l=accL.astype(np.float32), acc_r=accR.astype(np.float32),
              sr=sr)
    return mix, gt


# ---------------------------------------------------------------- 指标

def si_sdr(est, ref):
    """尺度无关 SDR（dB）。"""
    est = np.asarray(est, dtype=np.float64).reshape(-1)
    ref = np.asarray(ref, dtype=np.float64).reshape(-1)
    m = min(len(est), len(ref))
    est, ref = est[:m], ref[:m]
    est = est - est.mean(); ref = ref - ref.mean()
    a = np.dot(est, ref) / (np.dot(ref, ref) + 1e-12)
    tgt = a * ref
    err = est - tgt
    return 10.0 * np.log10((np.dot(tgt, tgt) + 1e-12) / (np.dot(err, err) + 1e-12))


def evaluate(out_L, out_R, stems, gt, verbose=True):
    """对分离结果打一组分。"""
    n = len(gt["male"])
    rng = np.arange(n)
    # 只看"有人声"的段落（前奏/间奏的伴奏保真度另算）
    voiced = np.zeros(n, dtype=bool)
    for a, b in ((8, 24), (24, 40), (40, 56), (64, 72)):
        voiced[a * gt["sr"]:min(n, b * gt["sr"])] = True

    res = {}
    res["male_stem"] = si_sdr(stems["male"][voiced], gt["male"][voiced])
    res["female_stem"] = si_sdr(stems["female"][voiced], gt["female"][voiced])
    # 目标：男声在左、女声在右、伴奏原样
    tgt_L = 0.9 * gt["acc_l"] + 0.55 * gt["male"]
    tgt_R = 0.9 * gt["acc_r"] + 0.55 * gt["female"]
    res["panned_L"] = si_sdr(out_L[voiced], tgt_L[voiced])
    res["panned_R"] = si_sdr(out_R[voiced], tgt_R[voiced])
    # 人声段的"左右去相关"：负得越多说明人声被拉得越开
    res["corr_out"] = float(np.corrcoef(out_L[voiced], out_R[voiced])[0, 1])
    res["corr_in"] = float(np.corrcoef(0.9 * gt["acc_l"] + 0.55 * (gt["male"] + gt["female"]),
                                       0.9 * gt["acc_r"] + 0.55 * (gt["male"] + gt["female"]))[0, 1])
    # 伴奏段（前奏 0-8 s）不能被动过
    inst = np.zeros(n, dtype=bool)
    inst[0:8 * gt["sr"]] = True
    # 原来是 `...[0:0].size or 0`：切片 [0:0] 长度恒为 0，所以这个指标恒为 0，
    # 用它得出的"伴奏没被动过"结论全是假的。改成真正的最大偏差。
    m = min(out_L.shape[0], gt["acc_l"].shape[0])
    inst_m = inst[:m]
    res["inst_maxdiff"] = float(np.max(np.abs(
        out_L[:m][inst_m] - 0.9 * gt["acc_l"][:m][inst_m]))) if m else 0.0
    if verbose:
        print('   男声轨 SI-SDR %+6.2f dB   女声轨 %+6.2f dB' %
              (res["male_stem"], res["female_stem"]))
        print('   左声道(男) %+6.2f dB   右声道(女) %+6.2f dB' %
              (res["panned_L"], res["panned_R"]))
        print('   人声段左右相关：原 %.3f -> 输出 %.3f' %
              (res["corr_in"], res["corr_out"]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default=None, help="把结果写成 wav 方便试听")
    args = ap.parse_args()

    print("生成二重唱基准 ...")
    mix, gt = make_duet()
    print("  混音 %s，%.0f 秒" % (mix.shape, len(mix) / SR))

    import dsp_core as dsp

    t0 = time.time()
    sp = dsp.VocalSplitter(SR)
    sp.configure(depth=1.0)
    y, stems = sp.process(mix, stems=True)
    dt = time.time() - t0
    print("当前实现：%.1f s（%.0f 倍实时）" % (dt, (len(mix) / SR) / dt))
    info = sp.summary()
    print("  统计：男 %.1f%% 女 %.1f%% 未判定 %.1f%%  分界 %.0f Hz"
          % (info["male_pct"], info["female_pct"], info["unvoiced_pct"], info["thr_hz"]))
    evaluate(y[:, 0], y[:, 1], stems, gt)

    if args.save:
        import audio_io
        audio_io.save_audio(args.save, y, SR)
        base = os.path.splitext(args.save)[0]
        for tag, k in (("male", "male"), ("female", "female"), ("acc", "accomp")):
            audio_io.save_audio("%s_%s.wav" % (base, tag), stems[k], SR)
        print("已写出", args.save)


if __name__ == "__main__":
    main()
