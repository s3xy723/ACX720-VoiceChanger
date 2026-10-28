# -*- coding: utf-8 -*-
"""chk_age_core.py —— 用 rtl/age_core.v 的仿真输出做判定。

判定逻辑全部复用 tool/sim_age.py 里已经验过的测量函数（est_beta 在合成元音
上有负对照：不处理时 β̂=1.000），RTL 只负责把数吐出来。

判据
----
  R1  β=1.0 时，对齐后的输出与输入相关系数 > 0.95（包络缩放必须透明）
  R2  β=1.25 时，独立测出的 β̂ 落在 1.10~1.40（共振峰确实上移）
  R3  β=1.25 时 F0 相对 β=1.0 的输出变化 < 3%（不能把基频带跑）
  R4  输出里没有 X / 全零（RTL 没跑飞）

用法
----
    cd /tmp/xtb && python C:/.../tool/chk_age_core.py out=out_b100.txt beta=1.0
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "tool"), os.path.join(ROOT, "host")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sim_age as S  # noqa: E402

SR = 24414.0625


def load(path):
    with open(path, "r") as f:
        v = [int(x) for x in f.read().split() if x.strip()]
    return np.asarray(v, dtype=np.float64) / 32768.0


def align_delay(x, y, maxlag=3000):
    """用互相关找出 RTL 的固定延迟（DELAY=1280 左右）。"""
    n = min(len(x), len(y))
    best, bl = -1e18, 0
    xs = x[:n]
    for lag in range(0, min(maxlag, n - 2000)):
        seg = y[lag:lag + (n - 2000)]
        c = float(np.dot(xs[:len(seg)], seg))
        if c > best:
            best, bl = c, lag
    return bl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--iname", default="in_b100.txt")
    ap.add_argument("--o100", default="out_b100.txt")
    ap.add_argument("--o125", default="out_b125.txt")
    a = ap.parse_args()

    ok = fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [PASS] %s %s" % (name, detail))
        else:
            fail += 1
            print("  [FAIL] %s %s" % (name, detail))

    def p(*args):
        return os.path.join(a.dir, *args)

    if not os.path.exists(p(a.o100)):
        print("  找不到 %s —— 先跑 sim_src/tb_age_core.v" % p(a.o100))
        return 1

    xin = load(p(a.iname))
    y100 = load(p(a.o100))

    print("chk_age_core：RTL 仿真输出判定")
    print("  输入 %d 样本，β=1.0 输出 %d 样本" % (len(xin), len(y100)))

    chk("R4 输出非全零、无异常", np.std(y100) > 1e-4 and np.isfinite(y100).all(),
        "std=%.4f peak=%.3f" % (np.std(y100), np.abs(y100).max()))

    lag = align_delay(xin, y100)
    # ★★ 这里原来写的是 ya = y100[:n]，**没有把延迟应用上去** ——
    #   align_delay 明明算出了 lag（477），后面却拿 x[i] 直接比 y[i]，
    #   于是无论 RTL 对不对，相关系数都只有 0.07。判据自己错了三、四轮。
    n = min(len(xin), len(y100) - lag)
    xa, ya = xin[:n], y100[lag:lag + n]
    # 跳过开机填充段（前 512 个样本缓冲还没满）
    skip = 700
    if n > skip + 200:
        xa, ya = xa[skip:], ya[skip:]
    corr = float(np.corrcoef(xa, ya)[0, 1]) if n > 10 else 0.0
    chk("R1 β=1.0 透明（相关 > 0.95，延迟 %d 样本）" % lag, corr > 0.95,
        "corr=%.4f" % corr)

    if os.path.exists(p(a.o125)):
        y125 = load(p(a.o125))
        n2 = min(len(y100), len(y125))
        if n2 > 2000:
            b = S.est_beta(y100[:n2], y125[:n2], SR)
            chk("R2 β=1.25 时共振峰确实上移", 1.10 <= b <= 1.40, "β̂=%.3f" % b)
            f0a = S.est_f0(y100[:n2], SR)
            f0b = S.est_f0(y125[:n2], SR)
            rel = abs(f0b / f0a - 1) if (f0a == f0a and f0b == f0b) else 1.0
            chk("R3 基频不被带跑（<3%）", rel < 0.03,
                "%.1f -> %.1f Hz（%.2f%%）" % (f0a, f0b, 100 * rel))
        else:
            print("  [跳过] β=1.25 的有效样本不足（%d）" % n2)
    else:
        print("  [跳过] 没有 %s" % p(a.o125))

    print("\n  PASS = %d  FAIL = %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
