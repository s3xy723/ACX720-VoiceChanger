#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""calib_f0.py —— 基频估计方案对比（这是整套分离算法的真正瓶颈）

来由
----
tool/calib_structural.py 的 [E] 实验给出了决定性的数字：

    同一把谐波梳（齿宽 18 Hz），
    喂**从 AI 人声估出来的真实基频** → SIR +22.38 dB、SI-SDR +9.30 dB
    喂**现在算法自己估的基频**     → SIR -1.53 dB、SI-SDR -1.18 dB

梳状掩蔽的结构完全没问题，24 dB 全丢在基频上。所以这里只测基频：

  参考真值  f0_true ：从 AI 人声（干净信号）用归一化自相关逐帧估
  候选方案          ：全部**只看混音的 mid**（实时/FPGA 只有这个可用）

    (a) 现在的做法：自相关，3000 Hz 低通 + 70~500 Hz 范围
    (b) 谱白化后再自相关（除以平滑包络，压掉宽带伴奏背景）
    (c) 先抠掉中置度低的部分（等于 vocal_split 里的 V）再自相关
    (d) 高通 150 Hz 后自相关（贝斯/底鼓是最大的干扰源）

指标：与 f0_true 相差 3% 以内算对；差 2:1 或 1:2 算八度错；
      自相关显著度不够、判成无浊音算漏判。

用法：
    python tool/calib_f0.py --secs 40:100
"""
from __future__ import print_function

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOP, "host"))
sys.path.insert(0, HERE)

from calib_fpga_sep import load_refs, parse_secs, si_sdr, project_sir  # noqa: E402
from calib_structural import stft, istft_ola, NFFT, HOP, WIN            # noqa: E402
from vocal_route import _acf_f0                                        # noqa: E402

FMIN, FMAX = 70.0, 500.0


def acf_variant(seg, sr, mode, fmin=FMIN, fmax=FMAX):
    """四种前端处理，最后都走同一套 _acf_f0 的自相关逻辑。"""
    s = np.asarray(seg, dtype=np.float64)
    if mode == "raw":
        return _acf_f0(s, sr, fmin, fmax)
    if mode == "hp150":
        # 一阶高通 150 Hz（零相位：正反各滤一次）
        a = np.exp(-2.0 * np.pi * 150.0 / sr)
        y = np.empty_like(s)
        prev_x = 0.0
        prev_y = 0.0
        for i in range(s.size):
            prev_y = a * (prev_y + s[i] - prev_x)
            prev_x = s[i]
            y[i] = prev_y
        y = y[::-1].copy()
        prev_x = 0.0
        prev_y = 0.0
        for i in range(y.size):
            prev_y = a * (prev_y + y[i] - prev_x)
            prev_x = y[i]
            y[i] = prev_y
        return _acf_f0(y[::-1], sr, fmin, fmax)
    if mode == "whiten":
        # 频域白化：除以滑动平均幅度包络，再变回时域
        n = s.size
        nfft = 1 << int(np.ceil(np.log2(2 * n)))
        S = np.fft.rfft(s * WIN[:n], nfft)
        f = np.fft.rfftfreq(nfft, 1.0 / sr)
        # 包络用 ~250 Hz 宽的滑动平均
        w = max(3, int(round(250.0 / (sr / float(nfft)))))
        mag = np.abs(S)
        env = np.convolve(mag, np.ones(w) / w, mode="same")
        S2 = S / (env + 1e-9)
        y = np.fft.irfft(S2, nfft)[:n]
        return _acf_f0(y, sr, fmin, fmax)
    if mode == "center":
        return _acf_f0(s, sr, fmin, fmax)      # 由调用方先传入已中置提取的信号
    raise ValueError(mode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", default="40:100")
    ap.add_argument("--sr", type=int, default=24414)
    args = ap.parse_args()

    sr_want = args.sr if args.sr > 0 else None
    mix, voc, acc, sr = load_refs("", sr_want, parse_secs(args.secs))
    n = len(mix)
    rv = 0.5 * (voc[:, 0] + voc[:, 1])
    ra = 0.5 * (acc[:, 0] + acc[:, 1])
    xm = 0.5 * (mix[:, 0] + mix[:, 1])
    xs = 0.5 * (mix[:, 0] - mix[:, 1])

    X = stft(xm)
    S = stft(xs)
    nfr = X.shape[0]
    M = np.abs(X)
    Sm = np.abs(S)
    freqs = np.fft.rfftfreq(NFFT, 1.0 / sr)

    print("=" * 96)
    print(" 基频估计方案对比：%d Hz, %.1f s, %d 帧" % (sr, n / float(sr), nfr))
    print("=" * 96)

    # ---- 真值：从 AI 人声（干净）估 ----
    print("\n先建真值（在 AI 人声上估，88% 以上应当是浊音）…", flush=True)
    f0_true = np.zeros(nfr)
    for t in range(nfr):
        s0 = t * HOP
        f0_true[t] = acf_variant(rv[s0:s0 + NFFT], sr, "raw")[0]
    print("  浊音帧占比 %.1f%%" % (100 * np.mean(f0_true > 0)), flush=True)

    # ---- 中置提取后的 mid（方案 c 的输入）----
    # 用 vocal_split 里的中置度公式，把 side 成分压掉
    c_ms = np.clip((M - Sm) / np.maximum(M, 1e-18), 0.0, 1.0)

    # ---- 各方案 ----
    modes = [("a 现在的做法（自相关 on mid）", "raw"),
             ("b 谱白化 + 自相关", "whiten"),
             ("c 中置提取后 + 自相关", "center"),
             ("d 高通 150 Hz + 自相关", "hp150")]
    results = {}
    print("\n  %-34s %8s %9s %9s %9s" % ("方案", "正确率", "八度错", "漏判", "中位误差"))
    for label, mode in modes:
        f0 = np.zeros(nfr)
        for t in range(nfr):
            s0 = t * HOP
            if mode == "center":
                seg = np.fft.irfft(c_ms[t] * X[t], NFFT)
                f0[t] = acf_variant(seg, sr, "center")[0]
            else:
                f0[t] = acf_variant(xm[s0:s0 + NFFT], sr, mode)[0]
        results[mode] = f0
        ok = oct_ = miss = 0
        errs = []
        for t in range(nfr):
            a, b = f0[t], f0_true[t]
            if b <= 0:
                continue
            if a <= 0:
                miss += 1
            elif abs(a / b - 1.0) < 0.03:
                ok += 1
                errs.append(abs(a / b - 1.0))
            elif abs(a / b - 2.0) < 0.06 or abs(a / b - 0.5) < 0.03:
                oct_ += 1
            else:
                pass
        nv = int(np.sum(f0_true > 0))
        print("  %-34s %7.1f%% %8.1f%% %8.1f%% %8.2f%%"
              % (label, 100.0 * ok / nv, 100.0 * oct_ / nv, 100.0 * miss / nv,
                 100.0 * float(np.median(errs)) if errs else 0.0), flush=True)

    # ---- 用各自的基频去建梳，看最终分离度 ----
    print("\n  用各自的基频建梳（齿宽 18 Hz，最高 11 kHz），测最终分离度")
    print("  %-34s %10s %10s %10s" % ("方案", "SI-SDR", "SIR", "捕获"))
    for label, mode in modes:
        f0 = results[mode]
        mk = np.zeros_like(M)
        # 中值滤波 5 帧，和算法一致（避免单帧跳变）
        f0s = f0.copy()
        for t in range(nfr):
            a = max(0, t - 2)
            b = min(nfr, t + 3)
            w = f0s[a:b][f0s[a:b] > 0]
            if w.size >= 3:
                f0[t] = float(np.median(w))
        for t in range(nfr):
            f0v = f0[t]
            if f0v <= 0:
                continue
            h = 1
            while h * f0v < 11000.0 and h <= 48:
                fc = h * f0v
                bw = max(18.0, fc * 0.01)
                np.maximum(mk[t], np.clip(2.0 - np.abs(freqs - fc) / bw,
                                          0.0, 1.0), out=mk[t])
                h += 1
        est = istft_ola(mk * X, n)
        s_v = si_sdr(est, rv)
        sir, cap = project_sir(est, rv, ra)
        print("  %-34s %+9.2f %+10.2f %+10.2f" % (label, s_v, sir, cap),
              flush=True)

    # ---- 上界：真基频 ----
    mk = np.zeros_like(M)
    for t in range(nfr):
        f0v = f0_true[t]
        if f0v <= 0:
            continue
        h = 1
        while h * f0v < 11000.0 and h <= 48:
            fc = h * f0v
            bw = max(18.0, fc * 0.01)
            np.maximum(mk[t], np.clip(2.0 - np.abs(freqs - fc) / bw,
                                      0.0, 1.0), out=mk[t])
            h += 1
    est = istft_ola(mk * X, n)
    print("  %-34s %+9.2f %+10.2f %+10.2f"
          % ("★ 真基频（上界，不可实现）", si_sdr(est, rv),
             project_sir(est, rv, ra)[0], project_sir(est, rv, ra)[1]), flush=True)
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
