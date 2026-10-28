# -*- coding: utf-8 -*-
"""tune_split_va.py —— 为「左=人声 / 右=伴奏」的中置分离调默认参数。

合成一段"人声居中 + 贝斯/底鼓也居中 + 吉他硬左右"的混音（真实歌曲就是
这样，纯中置提取最难的就是居中的低频伴奏），跑参数网格，用 SI-SDR 选
broadband / strength / harmonic / floor / gate_db 的默认值。

用法：python tool/tune_split_va.py
"""
import os
import sys
import itertools
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
import vocal_split as vs  # noqa: E402


def make_mix(sr=48000, dur=6.0, seed=7):
    rng = np.random.RandomState(seed)
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr

    # ---- 人声：基频缓慢滑动 + 谐波 + 共振峰包络 ----
    f0 = 132.0 + 12.0 * np.sin(2 * np.pi * 0.35 * t) + 5.0 * np.sin(2 * np.pi * 1.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    formants = [(700, 110), (1220, 150), (2600, 220)]
    voc = np.zeros(n)
    for h in range(1, 26):
        fh = float(np.mean(f0)) * h
        w = sum(np.exp(-0.5 * ((fh - fc) / bw) ** 2) for fc, bw in formants)
        a = (1.0 / h) * (0.25 + w)
        voc += a * np.sin(h * phase + 0.3 * h)
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * t)
    # 音节间隙：每 0.8 s 静音 0.12 s
    gate = (np.sin(2 * np.pi * 1.25 * t) > -0.55).astype(np.float64)
    gate = np.convolve(gate, np.ones(400) / 400.0, mode="same")
    voc *= env * gate
    voc = voc / (np.max(np.abs(voc)) + 1e-9) * 0.30

    # ---- 伴奏：居中的贝斯 + 底鼓，硬左右的吉他/踩镲 ----
    bass = 0.10 * np.sin(2 * np.pi * 82.0 * t) * (0.7 + 0.3 * np.sin(2 * np.pi * 2.0 * t))
    kick = np.zeros(n)
    for k in range(0, n, int(0.5 * sr)):
        m = min(n - k, int(0.18 * sr))
        tt = np.arange(m) / sr
        kick[k:k + m] += 0.16 * np.sin(2 * np.pi * (55 + 35 * np.exp(-tt * 30)) * tt) \
            * np.exp(-tt * 22)
    gtr_l = 0.055 * np.sin(2 * np.pi * 330.0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.9 * t))
    gtr_r = 0.055 * np.sin(2 * np.pi * 440.0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 1.1 * t + 1))
    hat = 0.018 * rng.randn(n)
    hat = np.convolve(hat, np.array([1.0, -0.9]), mode="same")
    hat_l = hat * 0.8
    hat_r = np.roll(hat, 137) * 0.8

    L = voc + bass + kick + gtr_l + hat_l
    R = voc + bass + kick + gtr_r + hat_r
    pk = max(np.max(np.abs(L)), np.max(np.abs(R)))
    L *= 0.82 / pk
    R *= 0.82 / pk
    voc_ref = voc * (0.82 / pk)
    acc_ref = 0.5 * ((L - voc_ref) + (R - voc_ref))
    return np.stack([L, R], axis=1).astype(np.float32), voc_ref, acc_ref


def si_sdr(est, ref):
    est = np.asarray(est, dtype=np.float64)
    ref = np.asarray(ref, dtype=np.float64)
    est = est - est.mean()
    ref = ref - ref.mean()
    den = float(np.dot(ref, ref)) + 1e-12
    a = float(np.dot(est, ref)) / den
    target = a * ref
    noise = est - target
    return 10.0 * np.log10((float(np.dot(target, target)) + 1e-12) /
                           (float(np.dot(noise, noise)) + 1e-12))


def main():
    sr = 48000
    x, voc_ref, acc_ref = make_mix(sr)
    print("mix peak %.3f  vocal rms %.4f  acc rms %.4f"
          % (np.max(np.abs(x)), np.sqrt((voc_ref ** 2).mean()),
             np.sqrt((acc_ref ** 2).mean())))
    print("baseline (不分离): vox SI-SDR %.2f dB / acc SI-SDR %.2f dB"
          % (si_sdr(0.5 * (x[:, 0] + x[:, 1]), voc_ref),
             si_sdr(0.5 * (x[:, 0] + x[:, 1]), acc_ref)))

    rows = []
    grid = itertools.product([0.0, 0.10, 0.20, 0.25, 0.35, 0.50],  # broadband
                             [0.8, 1.0, 1.3],                     # strength
                             [True, False])                       # harmonic
    t0 = time.time()
    for bb, st, harm in grid:
        sp = vs.VocalSplitter(sr)
        sp.configure(depth=1.0, floor=0.0, strength=st,
                     broadband=bb, harmonic=harm, gate_db=-35.0)
        y = sp.process(x, block=8192)
        vsdr = si_sdr(y[:, 0], voc_ref)
        asdr = si_sdr(y[:, 1], acc_ref)
        rows.append((vsdr + asdr, vsdr, asdr, bb, st, harm))
        print("bb=%.2f st=%.1f harm=%d -> vox %+.2f  acc %+.2f  sum %+.2f"
              % (bb, st, harm, vsdr, asdr, vsdr + asdr))

    rows.sort(reverse=True)
    print("\n=== 粗网格 top 6（用时 %.1fs）===" % (time.time() - t0))
    for r in rows[:6]:
        print("sum %+.2f  vox %+.2f  acc %+.2f  | bb=%.2f strength=%.1f harmonic=%s"
              % (r[0], r[1], r[2], r[3], r[4], r[5]))

    best = rows[0]
    print("\n=== 用 top1 继续细调 floor / gate_db ===")
    fine = []
    for floor, gate in itertools.product([0.0, 0.03, 0.06, 0.10],
                                         [-45.0, -40.0, -35.0, -30.0, -25.0]):
        sp = vs.VocalSplitter(sr)
        sp.configure(depth=1.0, floor=floor, strength=best[4],
                     broadband=best[3], harmonic=best[5], gate_db=gate)
        y = sp.process(x, block=8192)
        v = si_sdr(y[:, 0], voc_ref)
        a = si_sdr(y[:, 1], acc_ref)
        fine.append((v + a, v, a, floor, gate))
        print("floor=%.2f gate=%.0f -> vox %+.2f acc %+.2f sum %+.2f"
              % (floor, gate, v, a, v + a))
    fine.sort(reverse=True)
    print("\n=== 细调 top 5 ===")
    for r in fine[:5]:
        print("sum %+.2f  vox %+.2f  acc %+.2f  | floor=%.2f gate=%.0f"
              % (r[0], r[1], r[2], r[3], r[4]))


if __name__ == "__main__":
    main()
