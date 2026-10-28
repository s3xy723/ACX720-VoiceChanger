# -*- coding: utf-8 -*-
"""
gen_fft_tb_data.py —— 给 fft1024 的裸仿真准备激励

之前 sim_fft1024.py 只是"Python 复刻 vs numpy"，RTL 本体从没在仿真里跑过。
这里补上：把已知的 1024 点送进 rtl/fft1024.v，输出和模型逐点对。

生成 sim_src/tb_fft_in_re.hex / _im.hex（16 进制补码，24 位 -> 6 位十六进制）。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import sim_fft1024 as F

OUT = os.path.join(ROOT, "sim_src")
N = F.N


def to_hex_s24(v):
    return "%06x" % (int(v) & 0xFFFFFF)


def main():
    rng = np.random.RandomState(11)
    t = np.arange(N)

    cases = []
    # 1) 单频正弦（硬件里真正的用法：16 位音频左移 1 位）
    k0 = 40
    x = np.round(12000.0 * np.sin(2 * np.pi * k0 * t / N)).astype(np.int64) * 2
    cases.append(("sine", x << 0, np.zeros(N, dtype=np.int64)))
    # 2) 复随机谱（顺带验 IFFT 那条路）
    cr = np.round(rng.randn(N) * 3000).astype(np.int64)
    ci = np.round(rng.randn(N) * 3000).astype(np.int64)
    cases.append(("cplx", cr, ci))

    with open(os.path.join(OUT, "tb_fft_in_re.hex"), "w") as f:
        for v in cases[0][1]:
            f.write(to_hex_s24(v) + "\n")
    with open(os.path.join(OUT, "tb_fft_in_im.hex"), "w") as f:
        for v in cases[0][2]:
            f.write(to_hex_s24(v) + "\n")

    # 参考
    with open(os.path.join(OUT, "tb_fft_ref.txt"), "w") as f:
        for name, re_, im_ in cases:
            for inv in (0, 1):
                r, i = F.fft_fixed(re_, im_, inverse=bool(inv))
                f.write("CASE %s inv=%d\n" % (name, inv))
                for j in range(N):
                    f.write("%d %d\n" % (int(r[j]), int(i[j])))
    print("wrote %s/tb_fft_in_*.hex + tb_fft_ref.txt" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
