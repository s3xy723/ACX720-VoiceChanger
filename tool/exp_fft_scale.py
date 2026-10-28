# -*- coding: utf-8 -*-
"""扫一下"从第几级开始缩放"对往返精度的影响，并检查是否溢出。"""
import math
import numpy as np

N = 1024
NB = 10
LIM = 2 ** 23          # 24 位有符号上限


def q15(x):
    v = int(round(x * 32767.0))
    return max(-32768, min(32767, v))


TW_RE = [q15(math.cos(-2.0 * math.pi * k / N)) for k in range(N // 2)]
TW_IM = [q15(math.sin(-2.0 * math.pi * k / N)) for k in range(N // 2)]


def bitrev(i):
    return int(("{:010b}".format(i))[::-1], 2)


def fft_fixed(x_re, x_im, scale_from, inverse=False, W=24):
    re = [int(v) for v in x_re]
    im = [int(v) for v in x_im]
    if inverse:
        im = [-v for v in im]
    peak = 0
    for s in range(NB):
        half = N >> (s + 1)
        blk = 2 * half
        sc = (s >= scale_from)
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
                sh_re = (d_re * c - d_im * d + 16384) >> 15
                sh_im = (d_re * d + d_im * c + 16384) >> 15
                if sc:
                    re[i0] = s_re >> 1
                    im[i0] = s_im >> 1
                    re[i1] = sh_re >> 1
                    im[i1] = sh_im >> 1
                else:
                    re[i0], im[i0] = s_re, s_im
                    re[i1], im[i1] = sh_re, sh_im
        peak = max(peak, max(max(abs(v) for v in re), max(abs(v) for v in im)))
    o_re = [re[bitrev(i)] for i in range(N)]
    o_im = [im[bitrev(i)] for i in range(N)]
    if inverse:
        o_im = [-v for v in o_im]
    return (np.array(o_re, dtype=np.int64), np.array(o_im, dtype=np.int64),
            peak, 1 << (NB - scale_from))


rng = np.random.RandomState(3)
xr = (rng.randn(N) * 8000).astype(np.int64)
x0 = xr << 1                       # 16 位音频左移 1 位，幅度 ≤ 2^16

print("输入幅度 max=%d" % int(np.abs(x0).max()))
print("%-8s %-10s %-12s %-16s %-14s" % ("scaleFrom", "前向缩放", "前向峰值", "往返最大误差", "相对误差"))
for sf in (0, 1, 2, 3, 4, 5):
    try:
        A_re, A_im, pk1, fscale = fft_fixed(x0, np.zeros(N, dtype=np.int64), sf)
        # 逆变换用同样的 scale_from
        B_re, B_im, pk2, iscale = fft_fixed(A_re, A_im, sf, inverse=True)
        # A = DFT(x0)/fscale ; B = iscale*IDFT(A) = iscale*x0/fscale
        back = B_re.astype(np.float64) * fscale / iscale / 2.0
        err = float(np.abs(back - xr).max())
        rel = 100.0 * err / float(np.abs(xr).max())
        ovf = "溢出!" if pk1 >= LIM or pk2 >= LIM else ""
        print("%-10d %-12d %-14d %-16.1f %7.3f%%  %s"
              % (sf, fscale, pk1, err, rel, ovf))
    except Exception as e:
        print(sf, "error", e)

# 最坏情况：直流 / 交变
print("\n最坏情况（直流满量程 / 交变满量程）：")
for name, sig in (("DC", np.full(N, 32767 * 2, dtype=np.int64)),
                  ("alt", np.array([1 if i % 2 == 0 else -1 for i in range(N)]) * 32767 * 2)):
    for sf in (3, 4, 5, 6):
        _, _, pk1, _ = fft_fixed(sig, np.zeros(N, dtype=np.int64), sf)
        print("  %-4s scaleFrom=%d 前向峰值=%d %s"
              % (name, sf, pk1, "溢出!" if pk1 >= LIM else "OK"))
