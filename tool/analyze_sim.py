# -*- coding: utf-8 -*-
"""
分析 tb_top.v 仿真产出的 sim_out_*.txt。

对每个阶段做自相关基频估计，和理论值对比，判断 RTL 实际行为是否符合预期。
用法: python tool/analyze_sim.py [输出目录]
"""
import math
import os
import sys

FS = 50_000_000.0 / 1024.0        # 48 828.125 Hz
F_IN = FS * 591.0 / 65536.0       # TB 里用的 FSTEP = 591


def read_samples(path):
    """读样本文件。仿真里可能出现 x/z，直接跳过但记数。"""
    v = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                v.append(int(ln))
            except ValueError:
                skipped += 1
    if skipped:
        print("  [%s] 跳过 %d 个非数值样本" % (os.path.basename(path), skipped))
    return v


def autocorr_f0(y, fs, lo=60.0, hi=1500.0, maxn=16384):
    n = min(len(y), maxn)
    if n < 1024:
        return 0.0
    seg = y[:n]
    mean = sum(seg) / float(n)
    sig = [x - mean for x in seg]
    e0 = sum(x * x for x in sig)
    if e0 <= 0:
        return 0.0
    lag_min = max(2, int(fs / hi))
    lag_max = min(int(fs / lo), n // 2)
    if lag_max <= lag_min:
        return 0.0
    rs = []
    for lag in range(lag_min, lag_max):
        acc = 0.0
        for i in range(n - lag):
            acc += sig[i] * sig[i + lag]
        rs.append(acc / float(e0))
    best_i = max(range(len(rs)), key=lambda i: rs[i])
    best_r = rs[best_i]
    if best_r <= 0:
        return 0.0
    # 取最早的显著峰，避免倍频锁定
    first = best_i
    for i in range(len(rs)):
        if rs[i] > 0.9 * best_r:
            first = i
            break
    lag = lag_min + first
    # 抛物线细化
    if 0 < first < len(rs) - 1:
        a, b, c = rs[first - 1], rs[first], rs[first + 1]
        den = (a - 2.0 * b + c)
        if den != 0:
            lag = lag_min + first + 0.5 * (a - c) / den
    return fs / lag if lag > 0 else 0.0


def rms(y):
    return math.sqrt(sum(x * x for x in y) / float(len(y))) if y else 0.0


EXPECT = {
    "00_bypass":      ("直通",        1.0,  None),
    "01_pitch_r1":    ("变调 r=1.0",  1.0,  None),
    "02_pitch_r2":    ("变调 r=2.0",  2.0,  None),
    "03_pitch_r0p5":  ("变调 r=0.5",  0.5,  None),
    "04_ringmod":     ("环形调制100Hz", None, 100.0),
}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.normpath(
        os.path.join(here, "..", "vivado_prj"))

    # 仿真输出文件可能落在 vivado 运行目录，扫一下常见位置
    found = {}
    for d in (root, os.path.normpath(os.path.join(here, "..")), "."):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.startswith("sim_out_") and fn.endswith(".txt"):
                key = fn[len("sim_out_"):-len(".txt")]
                found.setdefault(key, os.path.join(d, fn))

    if not found:
        print("没找到 sim_out_*.txt，先跑仿真。搜索目录: %s" % root)
        return

    print("输入频率 %.2f Hz   fs = %.3f Hz" % (F_IN, FS))
    print("-" * 74)
    print("%-16s %-14s %10s %10s %8s %9s" %
          ("phase", "说明", "理论Hz", "实测Hz", "误差%", "RMS"))
    print("-" * 74)

    for key in sorted(found):
        y = read_samples(found[key])
        # 丢掉开头用于填满延迟线的部分
        tail = y[2200:] if len(y) > 3000 else y
        f0 = autocorr_f0(tail, FS)
        name, ratio, ringf = EXPECT.get(key, ("?", None, None))
        if ratio is not None:
            theo = ratio * F_IN
            err = abs(f0 - theo) / theo * 100.0 if theo else 0.0
            tstr = "%10.1f" % theo
            estr = "%8.2f" % err
        else:
            theo = None
            tstr = "%10s" % "-"
            estr = "%8s" % "-"
        print("%-16s %-14s %s %10.1f %s %9.1f" %
              (key, name, tstr, f0, estr, rms(tail)))

    print("-" * 74)
    print("注: 环形调制会把频谱搬到 f_in ± 100 Hz 的侧带上，")
    print("    自相关基频不再是单一值，用 RMS 判断有没有输出即可。")


if __name__ == "__main__":
    main()
