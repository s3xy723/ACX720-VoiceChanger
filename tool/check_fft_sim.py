# -*- coding: utf-8 -*-
"""
check_fft_sim.py —— 比对 tb_fft1024 的仿真输出与 sim_fft1024 的模型

RTL 里的数据通路宽度是 24 位（截断，不饱和），而 Python 模型用任意精度整数。
只要中间量不超 24 位两者就应该**逐点相等**；不等就说明要么 RTL 与模型
没对齐，要么中途溢出了（两者都要抓）。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import sim_fft1024 as F

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


def load_out(path):
    """返回 [(inv, arr(N,2)), ...]"""
    out = []
    inv = None
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        if line.startswith("CASE"):
            if inv is not None:
                out.append((inv, np.array(rows, dtype=np.int64)))
            inv = int(line.split("inv=")[1])
            rows = []
            continue
        rows.append([int(v) for v in line.split()])
    if inv is not None:
        out.append((inv, np.array(rows, dtype=np.int64)))
    return out


def load_ref(path):
    out = []
    name = None
    inv = None
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        if line.startswith("CASE"):
            if name is not None:
                out.append((name, inv, np.array(rows, dtype=np.int64)))
            parts = line.split()
            name = parts[1]
            inv = int(parts[2].split("=")[1])
            rows = []
            continue
        rows.append([int(v) for v in line.split()])
    if name is not None:
        out.append((name, inv, np.array(rows, dtype=np.int64)))
    return out


def main():
    sim = load_out(os.path.join(SIM, "tb_fft_out.txt"))
    ref = load_ref(os.path.join(SIM, "tb_fft_ref.txt"))
    print("=" * 70)
    print(" fft1024 —— RTL 仿真 vs 定点模型")
    print("=" * 70)
    check("仿真输出存在", len(sim) >= 2, "%d 段" % len(sim))

    # 仿真只跑了 sine 这一组激励（正/反），参考里对应 sine 的 inv=0/1
    for (inv, arr) in sim:
        cand = [r for (nm, iv, r) in ref if nm == "sine" and iv == inv]
        if not cand:
            check("找到对应参考 inv=%d" % inv, False)
            continue
        a = cand[0]
        d_re = int(np.abs(arr[:, 0] - a[:, 0]).max())
        d_im = int(np.abs(arr[:, 1] - a[:, 1]).max())
        pk = int(np.abs(a).max())
        print("    inv=%d  |Δre|=%-6d |Δim|=%-6d  模型峰=%-8d 仿真峰=%-8d"
              % (inv, d_re, d_im, pk, int(np.abs(arr).max())))
        check("inv=%d 与模型逐点一致" % inv, d_re == 0 and d_im == 0,
              "偏差 %d / %d" % (d_re, d_im))

    print("\n" + "=" * 70)
    print(" PASS = %d    FAIL = %d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
