#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""calib_structural.py —— 判断"调参数能不能救"，还是"算法框架本身要换"

起因
----
用真实歌曲《想把我唱给你听》跑基线后，FPGA 那套 M/S 中置掩蔽的人声 SIR
只有 **-0.40 dB**（等于没分离）。看起来像"参数没调好"，但实测这首歌的
**伴奏有 88.4% 的能量是居中的**（贝斯段 98% 居中）——

    M/S 类算法只能分"居中 vs 展开"。伴奏既然几乎全居中，它和人声在
    频域上是完全重叠的，再好的掩蔽公式也分不开。

所以本脚本做三件事，把"能不能救"这个问题彻底定量：

  [A] 天花板：任意**实数逐频点掩蔽**作用在 mid 上的最优解
      （逐帧变分最优掩蔽 M*=Re(V·conj(X))/|X|²，直接拿 AI 的真值算）
      → 如果连天花板都很低，说明 mid 这个观测里人声和伴奏根本不可分。

  [B] 当前公式的参数上限（M/S 中置度 + 梳状）
      → 扫描 floor / strength / gate，看最好也就多少。

  [C] 换观测：**时间维的平稳性**（REPET 思想）
      伴奏是重复/准平稳的，人声是变化的。逐频点跟踪一个"背景基线"，
      人声 = 超出基线的部分。这条**与声像无关**，所以居中伴奏也适用。
      用"快降慢升"的单极点跟踪器实现 —— 每频点每帧 3 次运算，
      FPGA 完全跑得动（比排序/中值便宜几个数量级）。

指标统一用：SI-SDR（越小越像 AI 那一轨）+ SIR（目标 vs 干扰的能量比）。

用法：
    python tool/calib_structural.py --secs 40:100
    python tool/calib_structural.py --secs 40:100 --sr 44100
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

from calib_fpga_sep import (load_refs, parse_secs, si_sdr, project_sir,
                            print_bands)          # noqa: E402

NFFT = 1024
HOP = 256
_OLA = 2.0 / 3.0


def hann(n):
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)


WIN = hann(NFFT)


def stft(x):
    """返回 (帧数, NFFT/2+1) 的复数谱。帧网格锚定在 k*HOP，与 RTL 一致。"""
    n = len(x)
    nfr = max(0, (n - NFFT) // HOP + 1)
    out = np.empty((nfr, NFFT // 2 + 1), dtype=np.complex128)
    for k in range(nfr):
        s = k * HOP
        out[k] = np.fft.rfft(x[s:s + NFFT] * WIN)
    return out


def istft_ola(X, n_out):
    """加权重叠相加，返回长度 n_out 的实信号。"""
    nfr = X.shape[0]
    y = np.zeros(n_out + NFFT, dtype=np.float64)
    wsum = np.zeros(n_out + NFFT, dtype=np.float64)
    for k in range(nfr):
        s = k * HOP
        seg = np.fft.irfft(X[k], NFFT) * WIN * _OLA
        y[s:s + NFFT] += seg
        wsum[s:s + NFFT] += WIN * WIN * _OLA
    # 稳态 Σw²·OLA = 1，边缘按实际权重归一，避免首尾凹陷
    wsum = np.maximum(wsum, 0.25)
    return y[:n_out] / wsum[:n_out]


def report(name, est, rv, ra, sr=0, show_bands=False):
    s_v = si_sdr(est, rv)
    sir, cap = project_sir(est, rv, ra)
    print("  %-42s  SI-SDR %+7.2f dB   SIR %+7.2f dB   捕获 %+6.2f dB"
          % (name, s_v, sir, cap), flush=True)
    if show_bands:
        print_bands(est, rv, sr, name)
    return s_v, sir, cap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", default="40:100")
    ap.add_argument("--sr", type=int, default=24414)
    ap.add_argument("--bands", action="store_true")
    args = ap.parse_args()

    sr_want = args.sr if args.sr > 0 else None
    mix, voc, acc, sr = load_refs("", sr_want, parse_secs(args.secs))
    n = len(mix)
    rv = 0.5 * (voc[:, 0] + voc[:, 1])          # AI 人声的 mid（FPGA 结构上能看到的）
    ra = 0.5 * (acc[:, 0] + acc[:, 1])
    xm = 0.5 * (mix[:, 0] + mix[:, 1])
    xs = 0.5 * (mix[:, 0] - mix[:, 1])

    print("=" * 96)
    print(" 结构化诊断：%d Hz, %.1f s" % (sr, n / float(sr)))
    print("=" * 96)

    X = stft(xm)
    S = stft(xs)
    V = stft(rv)                                 # AI 人声的 mid 谱（真值）
    M = np.abs(X)
    Sm = np.abs(S)
    Vm = np.abs(V)
    nfr = X.shape[0]
    print("  STFT：%d 帧 × %d bins（NFFT=%d, HOP=%d）" % (nfr, M.shape[1], NFFT, HOP))

    # ------------------------------------------------------------ 参照
    print("\n[0] 参照")
    report("原样直通（不做任何分离）", xm, rv, ra)
    report("理想：AI 人声 mid 原样输出", rv, rv, ra)

    # ------------------------------------------------------------ [A] 天花板
    print("\n[A] 天花板：任意实数逐频点掩蔽作用在 mid 上的最优解")
    # 对每个 (帧, 频点) 最小化 ||M*X - V||²，实数 M 的解是 M* = Re(V·conj(X))/|X|²
    den = np.abs(X) ** 2
    Mopt = np.real(V * np.conj(X)) / np.maximum(den, 1e-18)
    Mopt = np.clip(Mopt, 0.0, 4.0)               # 允许少量过冲（过减）
    est = istft_ola(Mopt * X, n)
    report("逐帧最优实数掩蔽（用真值算，不可实现）", est, rv, ra,
           sr, args.bands)

    # ------------------------------------------------------------ [B] 现有公式
    print("\n[B] 现有 M/S 中置度公式的参数上限（floor / strength / gate）")
    best = None
    for floor in (0.0, 0.06, 0.2):
        for strength in (1.0, 1.5, 2.0):
            base = np.clip((M - Sm) / np.maximum(M, 1e-18), 0.0, 1.0) ** strength
            base = floor + (1.0 - floor) * base
            for gate_db in (-40.0, -25.0, -15.0):
                g = M.max() * (10.0 ** (gate_db / 20.0))
                m2 = base * np.clip((M - g) / max(g, 1e-18), 0.0, 1.0)
                est = istft_ola(m2 * X, n)
                s_v = si_sdr(est, rv)
                sir, cap = project_sir(est, rv, ra)
                if best is None or s_v > best[0]:
                    best = (s_v, sir, cap, floor, strength, gate_db)
    print("  最优组合：floor=%.2f strength=%.1f gate=%.0f dB  "
          "→ SI-SDR %+7.2f dB  SIR %+7.2f dB  捕获 %+6.2f dB"
          % (best[3], best[4], best[5], best[0], best[1], best[2]), flush=True)

    # ------------------------------------------------------------ [C] 换观测
    print("\n[C] 换观测：逐频点时间平稳性跟踪（REPET 思想，与声像无关）")
    print("    单极点跟踪器：B = |X|（当 |X|<B）；否则 B += (|X|-B)>>k")
    print("    mask = clip(1 - g·B/|X| , 0, 1)^p")
    print("    %-34s %10s %10s %10s" % ("参数", "SI-SDR", "SIR", "捕获"))
    rows = []
    for k_up in (7, 8, 9, 10):
        for g in (1.0, 1.5, 2.0, 3.0):
            for p in (1.0, 2.0):
                alpha_up = 1.0 - 2.0 ** (-k_up)
                B = np.zeros(M.shape[1], dtype=np.float64)
                mask = np.empty_like(M)
                for t in range(nfr):
                    v = M[t]
                    down = v < B
                    B = np.where(down, v, B + (v - B) * alpha_up)
                    mask[t] = np.clip(1.0 - g * B / np.maximum(v, 1e-18),
                                      0.0, 1.0) ** p
                est = istft_ola(mask * X, n)
                s_v = si_sdr(est, rv)
                sir, cap = project_sir(est, rv, ra)
                rows.append((s_v, sir, cap, k_up, g, p, est))
                print("    %-34s %+9.2f %+10.2f %+10.2f"
                      % ("k_up=%d  g=%.1f  p=%.0f" % (k_up, g, p),
                         s_v, sir, cap), flush=True)
    rows.sort(key=lambda t: -t[0])
    print("    -> 最优 k_up=%d g=%.1f p=%.0f" % (rows[0][3], rows[0][4], rows[0][5]))
    if args.bands:
        print()
        print_bands(rows[0][6], rv, sr, "REPET 最优（人声）")

    # ------------------------------------------------------------ [D] 组合
    print("\n[D] 组合：M/S 中置度 × 时间平稳性（取小 = 两者都要求）")
    print("    %-34s %10s %10s %10s" % ("参数", "SI-SDR", "SIR", "捕获"))
    best_c = None
    k_up, g, p = rows[0][3], rows[0][4], rows[0][5]
    alpha_up = 1.0 - 2.0 ** (-k_up)
    B = np.zeros(M.shape[1], dtype=np.float64)
    mask_r = np.empty_like(M)
    for t in range(nfr):
        v = M[t]
        B = np.where(v < B, v, B + (v - B) * alpha_up)
        mask_r[t] = np.clip(1.0 - g * B / np.maximum(v, 1e-18), 0.0, 1.0) ** p
    base = np.clip((M - Sm) / np.maximum(M, 1e-18), 0.0, 1.0)
    for mode in ("min", "mul"):
        for w in (0.3, 0.5, 0.7, 1.0):
            mm = np.minimum(base ** w, mask_r) if mode == "min" \
                else (base ** w) * mask_r
            est = istft_ola(mm * X, n)
            s_v = si_sdr(est, rv)
            sir, cap = project_sir(est, rv, ra)
            if best_c is None or s_v > best_c[0]:
                best_c = (s_v, sir, cap, mode, w, est)
            print("    %-34s %+9.2f %+10.2f %+10.2f"
                  % ("%s(ms^%.1f, repet)" % (mode, w), s_v, sir, cap),
                  flush=True)
    print("    -> 最优 %s(ms^%.1f, repet)：SI-SDR %+7.2f  SIR %+7.2f  捕获 %+6.2f"
          % (best_c[3], best_c[4], best_c[0], best_c[1], best_c[2]), flush=True)
    if args.bands:
        print()
        print_bands(best_c[5], rv, sr, "组合最优（人声）")

    # ------------------------------------------------------------ [E] 谐波梳本身
    print("\n[E] 拆分「梳状设计」与「基频估计」：给梳子喂真实基频")
    print("    基频从 AI 人声（干净信号）用 FFT 自相关逐帧估，"
          "再拿它去掩蔽混音的 mid")
    print("    %-34s %10s %10s %10s" % ("齿宽 / 上限", "SI-SDR", "SIR", "捕获"))

    def acf_f0(seg, sr_, fmin=70.0, fmax=520.0):
        s = seg - seg.mean()
        e = float(np.dot(s, s))
        if e < 1e-10:
            return 0.0
        n2 = 2048
        S = np.fft.rfft(s, n2)
        ac = np.fft.irfft(np.abs(S) ** 2, n2)[:n2 // 2]
        if ac[0] <= 0:
            return 0.0
        ac = ac / ac[0]
        lo = max(2, int(sr_ / fmax))
        hi = min(len(ac) - 2, int(sr_ / fmin))
        if hi <= lo:
            return 0.0
        j = lo + int(np.argmax(ac[lo:hi]))
        if ac[j] < 0.25:                 # 周期性不明显就当无浊音
            return 0.0
        # 抛物线插值，避免整数 lag 的量化（24.4 kHz 下 1 拍 ≈ 5%）
        if 0 < j < len(ac) - 1:
            a, b, c = ac[j - 1], ac[j], ac[j + 1]
            d = a - 2 * b + c
            if abs(d) > 1e-12:
                j = j + 0.5 * (a - c) / d
        return float(sr_ / j) if j > 0 else 0.0

    f0_true = np.zeros(nfr)
    for t in range(nfr):
        s = t * HOP
        f0_true[t] = acf_f0(rv[s:s + NFFT], sr)
    voiced = float(np.mean(f0_true > 0))
    print("    浊音帧占比 %.1f%%（0 表示自相关判不出周期）" % (100 * voiced))

    freqs = np.fft.rfftfreq(NFFT, 1.0 / sr)
    for bw_min, fmax_h in ((35.0, 11000.0), (18.0, 11000.0), (9.0, 11000.0),
                           (18.0, 7000.0), (9.0, 7000.0)):
        mk = np.zeros_like(M)
        for t in range(nfr):
            f0 = f0_true[t]
            if f0 <= 0:
                continue
            h = 1
            while h * f0 < fmax_h and h <= 48:
                fc = h * f0
                bw = max(bw_min, fc * 0.01)
                np.maximum(mk[t], np.clip(2.0 - np.abs(freqs - fc) / bw,
                                          0.0, 1.0), out=mk[t])
                h += 1
        est = istft_ola(mk * X, n)
        s_v = si_sdr(est, rv)
        sir, cap = project_sir(est, rv, ra)
        print("    %-34s %+9.2f %+10.2f %+10.2f"
              % ("齿宽 %4.1f Hz, 最高 %5.0f Hz" % (bw_min, fmax_h),
                 s_v, sir, cap), flush=True)

    # ------------------------------------------------------------ 汇总
    print("\n" + "=" * 96)
    print(" 汇总（人声估计，基准 = AI 人声的 mid）")
    print("  %-38s %9s %9s" % ("方法", "SI-SDR", "SIR"))
    print("  %-38s %+9.2f %+9.2f" % ("[0] 直通（不处理）",
                                     si_sdr(xm, rv), project_sir(xm, rv, ra)[0]))
    print("  %-38s %+9.2f %+9.2f" % ("[A] 最优实数掩蔽（不可实现）",
                                     si_sdr(est if False else
                                            istft_ola(Mopt * X, n), rv),
                                     project_sir(istft_ola(Mopt * X, n), rv, ra)[0]))
    print("  %-38s %+9.2f %+9.2f" % ("[B] 现有公式调参上限", best[0], best[1]))
    print("  %-38s %+9.2f %+9.2f" % ("[C] 时间平稳性（REPET 类）",
                                     rows[0][0], rows[0][1]))
    print("  %-38s %+9.2f %+9.2f" % ("[D] 组合", best_c[0], best_c[1]))
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
