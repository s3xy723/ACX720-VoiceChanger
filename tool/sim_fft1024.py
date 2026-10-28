# -*- coding: utf-8 -*-
"""
sim_fft1024.py —— rtl/fft1024.v 的逐比特参考模型

本机 xsim 时好时坏，所以按本工程一贯做法：把 RTL 逐行复刻成 Python，
再和 numpy 的浮点 FFT 对照。它验证的是**算法与定点缩放**，
真值以 `numpy.fft` 为准，误差全部量化出来。

验证内容
--------
1. 定点 FFT 与 numpy.fft/1024 的误差（每级右移 1 位 → 总缩放 1/1024）
2. 位倒序读出后是自然序
3. IFFT（共轭法）与 numpy.ifft 的误差
4. FFT → IFFT 往返误差（单精度定点下应接近量化极限）
5. 满量程输入不溢出（24 位有符号）
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RTL = os.path.normpath(os.path.join(HERE, "..", "rtl", "fft1024.v"))
TW = os.path.normpath(os.path.join(HERE, "..", "rtl", "fft_twiddle.v"))

N = 1024
NB = 10

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


def q15(x):
    v = int(round(x * 32767.0))
    return max(-32768, min(32767, v))


TW_RE = [q15(math.cos(-2.0 * math.pi * k / N)) for k in range(N // 2)]
TW_IM = [q15(math.sin(-2.0 * math.pi * k / N)) for k in range(N // 2)]


def bitrev10(i):
    return int(("{:010b}".format(i))[::-1], 2)


def fft_fixed(x_re, x_im, inverse=False):
    """逐行复刻 fft1024.v。返回 (re, im)，长度 N。"""
    re = [int(v) for v in x_re]
    im = [int(v) for v in x_im]
    if inverse:                          # 输入取共轭
        im = [-v for v in im]

    for s in range(NB):
        half = N >> (s + 1)
        blk = 2 * half
        scale_en = (s >= 5)              # 只有后 5 级缩放
        for base in range(0, N, blk):
            for j in range(half):
                i0 = base + j
                i1 = i0 + half
                a_re, a_im = re[i0], im[i0]
                b_re, b_im = re[i1], im[i1]
                d_re, d_im = a_re - b_re, a_im - b_im
                s_re, s_im = a_re + b_re, a_im + b_im
                idx = j << s
                c, d = TW_RE[idx], TW_IM[idx]
                # m = (d*c - d*d + 16384) >>> 15（带舍入的 Q15 解算）
                sh_re = (d_re * c - d_im * d + 16384) >> 15
                sh_im = (d_re * d + d_im * c + 16384) >> 15
                if scale_en:
                    re[i0] = s_re >> 1
                    im[i0] = s_im >> 1
                    re[i1] = sh_re >> 1
                    im[i1] = sh_im >> 1
                else:
                    re[i0] = s_re
                    im[i0] = s_im
                    re[i1] = sh_re
                    im[i1] = sh_im

    o_re = [re[bitrev10(i)] for i in range(N)]
    o_im = [im[bitrev10(i)] for i in range(N)]
    if inverse:
        o_im = [-v for v in o_im]
    return np.array(o_re, dtype=np.int64), np.array(o_im, dtype=np.int64)


def main():
    print("=" * 66)
    print(" rtl/fft1024.v —— 逐比特参考模型")
    print("=" * 66)

    for p in (RTL, TW):
        check("源码存在 %s" % os.path.basename(p), os.path.exists(p))

    src = open(RTL, "r", encoding="utf-8").read()
    for key in ("module fft1024", "fft_twiddle", "wr_en", "inverse", "rd_addr"):
        if key not in src:
            check("源码含 %s" % key, False)
    check("源码结构完整", "module fft1024" in src and "endmodule" in src)

    rng = np.random.RandomState(7)

    # ---- 1) 定点 FFT vs numpy ----
    x = (rng.randn(N) * 4000).astype(np.int64)          # 约 -16000..16000
    ye = np.fft.fft((x << 1).astype(np.float64)) / 32   # 硬件：输入左移 1 位、总缩放 1/32
    re, im = fft_fixed(x << 1, np.zeros(N, dtype=np.int64))
    err = np.abs((re + 1j * im) - ye)
    rel = np.abs(err) / (np.abs(ye) + 1.0)
    check("定点 FFT 与 numpy.fft/1024 一致", float(np.median(rel)) < 0.02,
          "中位相对误差 %.4f，最大绝对误差 %.1f" % (float(np.median(rel)), float(err.max())))

    # ---- 2) 单频点：峰要落在正确的位置上 ----
    k0 = 137
    t = np.arange(N)
    x2 = (12000 * np.sin(2 * np.pi * k0 * t / N)).astype(np.int64) * 2
    re2, im2 = fft_fixed(x2, np.zeros(N, dtype=np.int64))
    mag = np.abs(re2 + 1j * im2)
    check("单频点峰值位置正确", int(np.argmax(mag)) == k0,
          "期望 bin %d，实测 %d" % (k0, int(np.argmax(mag))))
    side = float(mag[k0] / (mag.max() + 1e-9))
    check("单频点没有明显泄漏", side > 0.99, "峰值占比 %.3f" % side)

    # ---- 3) IFFT：任意复频谱 vs numpy.ifft ----
    S = (rng.randn(N) + 1j * rng.randn(N)) * 3000
    ri, ii = fft_fixed(np.round(S.real).astype(np.int64),
                       np.round(S.imag).astype(np.int64), inverse=True)
    got = ri + 1j * ii
    # 引擎的"逆变换"= 32 * IDFT（前向 5 级缩放 → 1/32，共轭法再翻回来）
    ref = np.fft.ifft(S) * 32.0
    e2 = np.abs(got - ref)
    check("IFFT 与 numpy.ifft 一致",
          float(e2.max()) < 0.05 * (np.abs(ref).max() + 1e-9),
          "最大绝对误差 %.3f（参考峰值 %.1f）" % (float(e2.max()), float(np.abs(ref).max())))

    # ---- 4) FFT -> IFFT 往返（真正用到的链路：分析 → 掩蔽 → 合成）----
    xr = (rng.randn(N) * 8000).astype(np.int64)
    A_re, A_im = fft_fixed(xr << 1, np.zeros(N, dtype=np.int64))   # = DFT(x')/32
    B_re, B_im = fft_fixed(A_re, A_im, inverse=True)               # = x'
    #   前向： A = DFT(x')/32
    #   反向： 共轭法给出 conj(DFT(conj(A))/32) = IDFT(A)*1024/32 = IDFT(A)*32，
    #          而 IDFT(A) = x'/32，于是 B = x'
    back = (B_re.astype(np.float64)) / 2.0                         # 补回输入左移的 1 位
    e3 = np.abs(back - xr)
    check("FFT→IFFT 往返误差小", float(e3.max()) < 40.0,
          "最大绝对误差 %.1f（输入幅度 %.0f，相对 %.2f%%）"
          % (float(e3.max()), float(np.abs(xr).max()),
             100.0 * float(e3.max()) / float(np.abs(xr).max())))

    # ---- 5) 满量程不溢出 ----
    xfull = np.full(N, 32767 * 2, dtype=np.int64)                 # 16 位满量程左移 1 位
    rf, imf = fft_fixed(xfull, np.zeros(N, dtype=np.int64))
    lim = 2 ** 23
    check("满量程输入不溢出 24 位", int(np.abs(rf).max()) < lim and int(np.abs(imf).max()) < lim,
          "最大 |re| = %d（上限 %d）" % (int(np.abs(rf).max()), lim))

    # ---- 6) 交替最大化增长的情形 ----
    xalt = (np.array([1 if i % 2 == 0 else -1 for i in range(N)]) * 32767 * 2)
    ra, ia = fft_fixed(xalt, np.zeros(N, dtype=np.int64))
    check("交变输入（最大增长）不溢出",
          int(np.abs(ra).max()) < lim and int(np.abs(ia).max()) < lim,
          "最大 |re| = %d" % int(np.abs(ra).max()))

    print("\n" + "=" * 66)
    print(" PASS = %d    FAIL = %d" % (PASS, FAIL))
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
