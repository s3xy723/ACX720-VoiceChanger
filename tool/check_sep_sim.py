# -*- coding: utf-8 -*-
"""
check_sep_sim.py —— separation_core 的 RTL/模型对数脚本

xsim 跑完 tb_separation 之后会留下：
    sim_src/tb_sep_pre.txt   每帧掩蔽前的 mid/side 谱（512 bin × 3）
    sim_src/tb_sep_acf.txt   每帧的自相关（1024 点）+ 判决结果
    sim_src/tb_sep_out.txt   输出样本

本脚本用 tool/sim_separation.py 的定点链路重算一遍同样的东西，
逐点对比。对不上就说明 RTL 和模型里有一处没对齐 —— 这正是要抓的。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import sim_separation as S

SIM = os.path.join(ROOT, "sim_src")

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


def sat16(v):
    return 32767 if v > 32767 else (-32768 if v < -32768 else v)


def load_hex(path):
    """hex 里存的是 16 位**补码**位型，必须还原符号。

    直接 int(x,16) 会拿到 0..65535，负数全变成大正数 ——
    那样 mid 能到 131068，模型侧的谱会比真实情况大一倍还多，
    和 RTL 对不上时根本看不出是谁错了。
    """
    v = np.array([int(x, 16) for x in open(path).read().split()],
                 dtype=np.int64)
    return (v ^ 0x8000) - 0x8000


def load_frames(path, ncols):
    """解析 'FRAME ws=N' 分段的文件，返回 [(ws, np.array([...])), ...]。"""
    out = []
    ws = None
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        if line.startswith("FRAME"):
            if ws is not None:
                out.append((ws, np.array(rows, dtype=np.int64)))
            kv = dict(p.split("=") for p in line.split()[1:])
            ws = int(kv.get("ws", "-1"))
            for k in ("acf0", "who", "lm", "lf"):
                if k in kv:
                    pass
            rows = []
            continue
        rows.append([int(v) for v in line.split()[:ncols]])
    if ws is not None:
        out.append((ws, np.array(rows, dtype=np.int64)))
    return out


def frame_meta(path):
    """从 acf 文件的 FRAME 头里取 (ws, acf0, who, lm, lf)。"""
    out = []
    for line in open(path):
        if line.startswith("FRAME"):
            kv = dict(p.split("=") for p in line.split()[1:])
            out.append((int(kv["ws"]), int(kv["acf0"]), int(kv["who"]),
                        int(kv["lm"]), int(kv["lf"])))
    return out


def main():
    S.load_recip()
    xl = load_hex(os.path.join(SIM, "tb_sep_in_l.hex"))
    xr = load_hex(os.path.join(SIM, "tb_sep_in_r.hex"))
    n = min(xl.size, xr.size)

    pre = load_frames(os.path.join(SIM, "tb_sep_pre.txt"), 4)
    acf = load_frames(os.path.join(SIM, "tb_sep_acf.txt"), 2)
    meta = frame_meta(os.path.join(SIM, "tb_sep_acf.txt"))

    print("=" * 70)
    print(" separation_core —— RTL 与定点参考模型对数")
    print("=" * 70)
    print("样本 %d，RTL 帧数 pre=%d acf=%d" % (n, len(pre), len(acf)))
    check("仿真确实产生了帧", len(pre) >= 4 and len(acf) >= 4,
          "pre=%d acf=%d" % (len(pre), len(acf)))
    if not pre:
        print("\n PASS = %d  FAIL = %d" % (PASS, FAIL))
        return 1

    # ---------------------------------------------------------- 1) mid/side 谱
    print("\n[1] 掩蔽前的 mid/side 谱（RTL vs 模型）")
    worst = 0
    # 注意：ws 是**缓冲区下标**，会绕回。第 k 帧分析的是绝对样本
    # [256k, 256k+1024)（分析窗恒取最近 1024 点），所以按帧序算 s，不能按 ws。
    for k, (ws, arr) in enumerate(pre):
        s = 256 * k
        if s + S.N > n:
            continue
        Vr, Vi = S.fixed_analyze(xl[ws:ws + S.N], xr[ws:ws + S.N])
        # Python 的 fixed_analyze 返回的是掩蔽**之后**的 V，
        # 这里要的是掩蔽之前的原始谱，所以单独重算一遍
        mid = ((xl[s:s + S.N] + xr[s:s + S.N]) >> 1) << 1
        sde = ((xl[s:s + S.N] - xr[s:s + S.N]) >> 1) << 1
        mw = np.array([S.sat((int(mid[i]) * int(S.WIN[i]) + 16384) >> 15, 24)
                       for i in range(S.N)], dtype=np.int64)
        sw = np.array([S.sat((int(sde[i]) * int(S.WIN[i]) + 16384) >> 15, 24)
                       for i in range(S.N)], dtype=np.int64)
        z = np.zeros(S.N, dtype=np.int64)
        A_re, A_im = S.fft_fixed(mw, z)
        B_re, B_im = S.fft_fixed(sw, z)
        sd = np.array([S.amax_beta(B_re[k], B_im[k]) for k in range(S.NBIN)],
                      dtype=np.int64)
        d_re = int(np.abs(arr[:, 1] - A_re[:S.NBIN]).max())
        d_im = int(np.abs(arr[:, 2] - A_im[:S.NBIN]).max())
        d_sd = int(np.abs(arr[:, 3] - sd).max())
        worst = max(worst, d_re, d_im, d_sd)
        print("    ws=%4d  |Δmid_re|=%-7d |Δmid_im|=%-7d |Δside|=%-7d"
              "  模型峰=%d RTL峰=%d"
              % (ws, d_re, d_im, d_sd,
                 int(np.abs(A_re[:S.NBIN]).max()),
                 int(np.abs(arr[:, 1]).max())))
    check("mid/side 谱与模型逐点一致", worst == 0, "最大偏差 %d" % worst)

    # ---------------------------------------------------------- 2) 自相关
    print("\n[2] |V|^2 -> IFFT -> 自相关（RTL vs 模型）")
    worst_a = 0
    amax_rtl = 0
    amax_py = 0
    for k, (ws, arr) in enumerate(acf):
        s = 256 * k
        if s + S.N > n:
            continue
        Vr, Vi = S.fixed_analyze(xl[s:s + S.N], xr[s:s + S.N])
        a = S.fixed_acf(Vr, Vi)
        d = int(np.abs(arr[:, 1] - a).max())
        worst_a = max(worst_a, d)
        amax_rtl = max(amax_rtl, int(np.abs(arr[:, 1]).max()))
        amax_py = max(amax_py, int(np.abs(a).max()))
        print("    ws=%4d  |Δacf|=%-9d  模型 acf0=%-8d RTL acf0=%-8d"
              % (ws, d, int(a[0]), int(arr[0, 1])))
    check("自相关与模型逐点一致", worst_a == 0, "最大偏差 %d（RTL 峰 %d / 模型峰 %d）"
          % (worst_a, amax_rtl, amax_py))

    # ---------------------------------------------------------- 3) 判决
    print("\n[3] 基频判决")
    ok_who = 0
    ok_lf = 0
    for k, (ws, a0, who, lm, lf) in enumerate(meta):
        s = 256 * k
        if s + S.N > n:
            continue
        Vr, Vi = S.fixed_analyze(xl[s:s + S.N], xr[s:s + S.N])
        a = S.fixed_acf(Vr, Vi)
        a0p = int(a[0]) if int(a[0]) > 0 else 1
        lm_p, pm = S.pick_peak(a, S.LAG_M_LO, S.LAG_M_HI)
        lf_p, pf = S.pick_peak(a, S.LAG_F_LO, S.LAG_F_HI)
        w_p, lm2, lf2 = S.decide(lm_p, pm, lf_p, pf, a0p, Vr, Vi, a)
        print("    ws=%4d  RTL who=%d lm=%3d lf=%3d | 模型 who=%d lm=%3d lf=%3d"
              % (ws, who, lm, lf, w_p, lm2, lf2))
        ok_who += (who == w_p)
        ok_lf += (abs(lf - lf2) <= 2)
    check("who 与模型一致", ok_who >= len(meta) - 1,
          "%d/%d 帧一致" % (ok_who, len(meta)))
    check("女声基频与模型一致（±2 迟滞）", ok_lf >= len(meta) - 1,
          "%d/%d 帧一致" % (ok_lf, len(meta)))

    # ---------------------------------------------------------- 4) 输出样本
    # dout_l[i] = xl[i] - sat16(acc_f[i] >> 1)
    # dout_r[i] = xr[i] - sat16(acc_m[i] >> 1)
    # 覆盖样本 i 的帧恰有 4 个：256k <= i < 256k+1024。
    print("\n[4] 输出样本（OLA + 减法）")
    # 只有一行输出时 np.loadtxt 返回一维数组，下面 row[0] 会抛
    # "numpy.int64 object is not subscriptable"，仿真刚跑起来时必崩。
    out = np.atleast_2d(
        np.loadtxt(os.path.join(SIM, "tb_sep_out.txt"), dtype=np.int64))
    if out.size == 0 or out.ndim != 2:
        check("有输出样本", False, "shape=%r" % (out.shape,))
        return 1

    frames = {}                                  # k -> (who, lm, lf, gm, gf)
    worst_l = 0
    worst_r = 0
    ncmp = 0
    for row in out:
        i = int(row[0])
        # tb 用**当前输入下标**给输出打标签，但输出的是 DELAY=1280 点**之前**
        # 的样本（这就是 DELAY 的意义）。所以源样本是 j = i-1280，不是 i。
        j = i - 1280
        # 前面的样本覆盖它的帧不足 4 个（还没有 k<0 的帧），跳过启动段
        if j < 1536 or j + S.N > n:
            continue
        acc_m = 0
        acc_f = 0
        for k in range(max(0, (j - S.N + 1) // S.HOP + 1), j // S.HOP + 1):
            if k not in frames:
                s = S.HOP * k
                Vr, Vi = S.fixed_analyze(xl[s:s + S.N], xr[s:s + S.N])
                a = S.fixed_acf(Vr, Vi)
                a0 = int(a[0]) if int(a[0]) > 0 else 1
                lm, pm = S.pick_peak(a, S.LAG_M_LO, S.LAG_M_HI)
                lf, pf = S.pick_peak(a, S.LAG_F_LO, S.LAG_F_HI)
                w, lm2, lf2 = S.decide(lm, pm, lf, pf, a0, Vr, Vi, a)
                gm, gf = S.fixed_mask_and_synth(Vr, Vi, w, lm2, lf2)
                frames[k] = (w, lm2, lf2, gm, gf)
            w, lm2, lf2, gm, gf = frames[k]
            t = j - S.HOP * k
            acc_m += int(gm[t])
            acc_f += int(gf[t])
        el = int(xl[j]) - sat16(acc_f >> S.OLA_SHIFT)
        er = int(xr[j]) - sat16(acc_m >> S.OLA_SHIFT)
        worst_l = max(worst_l, abs(el - int(row[1])))
        worst_r = max(worst_r, abs(er - int(row[2])))
        ncmp += 1
    print("    比对 %d 个样本：|ΔL|=%d  |ΔR|=%d" % (ncmp, worst_l, worst_r))
    check("左声道输出与模型一致", worst_l == 0, "最大偏差 %d" % worst_l)
    check("右声道输出与模型一致", worst_r == 0, "最大偏差 %d" % worst_r)

    print("\n" + "=" * 70)
    print(" PASS = %d    FAIL = %d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
