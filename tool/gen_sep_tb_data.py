# -*- coding: utf-8 -*-
"""
gen_sep_tb_data.py —— 给 separation_core 的 xsim 仿真准备激励

生成 sim_src/tb_sep_in_l.hex / tb_sep_in_r.hex（每行一个 16 位补码十六进制），
以及一份 Python 侧算好的"参考答案" tb_sep_ref.txt，供仿真结束后对数。
"""

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sim_separation as S

OUT = os.path.normpath(os.path.join(HERE, "..", "sim_src"))


def write_hex(path, ints):
    with open(path, "w") as f:
        for v in ints:
            f.write("%04x\n" % (int(v) & 0xFFFF))


def main():
    S.load_recip()
    n = 4096                                  # 约 4 帧的余量，够跑通状态机
    L, R, vm, vf = S.make_stereo(n, male=True, female=True, accomp=True,
                                 f_female=199.0)
    xl = np.round(L * 22000.0).astype(np.int64)
    xr = np.round(R * 22000.0).astype(np.int64)
    xl = np.clip(xl, -32768, 32767)
    xr = np.clip(xr, -32768, 32767)

    write_hex(os.path.join(OUT, "tb_sep_in_l.hex"), xl)
    write_hex(os.path.join(OUT, "tb_sep_in_r.hex"), xr)

    # 参考答案：用 Python 链路把 acc_m / acc_f 算出来
    acc_m = np.zeros(n, dtype=np.int64)
    acc_f = np.zeros(n, dtype=np.int64)
    who = []
    nfr = (n - S.N) // S.HOP + 1
    for k in range(nfr):
        s = k * S.HOP
        Vr, Vi = S.fixed_analyze(xl[s:s + S.N], xr[s:s + S.N])
        acf = S.fixed_acf(Vr, Vi)
        acf0 = int(acf[0]) if int(acf[0]) > 0 else 1
        lm, pm = S.pick_peak(acf, S.LAG_M_LO, S.LAG_M_HI)
        lf, pf = S.pick_peak(acf, S.LAG_F_LO, S.LAG_F_HI)
        w, lm2, lf2 = S.decide(lm, pm, lf, pf, acf0, Vr, Vi, acf)
        gm, gf = S.fixed_mask_and_synth(Vr, Vi, w, lm2, lf2)
        acc_m[s:s + S.N] += gm
        acc_f[s:s + S.N] += gf
        who.append((w, lm2, lf2))

    with open(os.path.join(OUT, "tb_sep_ref.txt"), "w") as f:
        f.write("# frame who lag_m lag_f\n")
        for i, (w, a, b) in enumerate(who):
            f.write("%d %d %d %d\n" % (i, w, a, b))
        f.write("# acc_m acc_f (24bit signed, sample idx)\n")
        for i in range(0, n, 64):
            f.write("%d %d %d\n" % (i, int(acc_m[i]), int(acc_f[i])))

    print("wrote hex + ref to", OUT)
    print("frames:", who[:8])
    return 0


if __name__ == "__main__":
    sys.exit(main())
