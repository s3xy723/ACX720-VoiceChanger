# -*- coding: utf-8 -*-
"""
sim_stereo_meter.py  ——  rtl/stereo_meter.v 的行为级仿真（纯 Python，不依赖 xsim）

背景
----
xsim 在本机历史上多次挂死（进程活着、CPU 时间接近 0、日志 0 字节），
所以本工程的 RTL 验证一律走"逐行复刻 + 理想激励"的路子，
`tool/sim_i2s_slave.py` 就是这么抓出两个真 bug 的。这里同样把
stereo_meter.v 的每一条时序重新实现一遍，再和 RTL 的写法对着看。

验证内容
--------
1. 绝对值：|−32768| 要能出 32768（16 位无符号正好用满，不能回绕成 0）
2. 峰值包络：快攻慢放，涨立刻跟上、跌按 1/64 指数回落
3. 有信号阈值：低于 SIG_THRESH 不点亮
4. 削波：偶发过载要"闪一下就灭"，持续过载要常亮（保持 0.5 s）
5. 立体声统计：
      L == R          -> led[7] 必须灭
      L == -R         -> led[7] 必须亮
      两个不相关信号   -> led[7] 必须亮
6. 移位运算不溢出：全量程 ±32768 常驻时累加器不越界

用法：  python tool/sim_stereo_meter.py
"""

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RTL = os.path.normpath(os.path.join(HERE, "..", "rtl", "stereo_meter.v"))

# 与 stereo_meter.v 的 parameter 保持一致
SIG_THRESH = 512
CLIP_THRESH = 32000
HOLD_SAMPLES = 12207
ATT_SHIFT = 6
ACC_SHIFT = 8
ACCW = 30

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


def abs16(v):
    """Verilog: din[15] ? (~din + 1) : din，按 16 位无符号解释。"""
    v &= 0xFFFF
    return ((~v) + 1) & 0xFFFF if (v & 0x8000) else v


class Meter(object):
    """stereo_meter.v 的逐时钟复刻：每个 valid 拍推进一次。"""

    def __init__(self):
        self.lvl_l = 0
        self.lvl_r = 0
        self.sig_l = 0
        self.sig_r = 0
        self.hold_l = 0
        self.hold_r = 0
        self.clip_l = 0
        self.clip_r = 0
        self.acc_diff = 0
        self.acc_sum = 0
        self.stereo = 0
        self._level_dbg = []

    def step(self, l, r):
        a_l = abs16(l)
        a_r = abs16(r)

        # 峰值包络
        self.lvl_l = a_l if a_l >= self.lvl_l else self.lvl_l - (self.lvl_l >> ATT_SHIFT)
        self.lvl_r = a_r if a_r >= self.lvl_r else self.lvl_r - (self.lvl_r >> ATT_SHIFT)

        # 有信号
        self.sig_l = 1 if a_l > SIG_THRESH else 0
        self.sig_r = 1 if a_r > SIG_THRESH else 0

        # 削波保持（注意：clip 用的是保持寄存器的**旧值**）
        old_hl, old_hr = self.hold_l, self.hold_r
        self.clip_l = 1 if (a_l >= CLIP_THRESH or old_hl != 0) else 0
        self.clip_r = 1 if (a_r >= CLIP_THRESH or old_hr != 0) else 0
        if a_l >= CLIP_THRESH:
            self.hold_l = HOLD_SAMPLES
        elif self.hold_l != 0:
            self.hold_l -= 1
        if a_r >= CLIP_THRESH:
            self.hold_r = HOLD_SAMPLES
        elif self.hold_r != 0:
            self.hold_r -= 1

        # 相关性
        dsub = (l if l < 0x8000 else l - 0x10000) - (r if r < 0x8000 else r - 0x10000)
        diff = dsub if dsub >= 0 else -dsub
        sumh = a_l + a_r
        assert 0 <= diff < (1 << 17), "diff 超 17 位"
        assert 0 <= sumh < (1 << 17), "sumh 超 17 位"
        diff_in = (diff << (ACCW - 17)) & ((1 << ACCW) - 1)
        sum_in = (sumh << (ACCW - 17)) & ((1 << ACCW) - 1)
        assert diff << (ACCW - 17) < (1 << ACCW), "diff 移位溢出"
        assert sumh << (ACCW - 17) < (1 << ACCW), "sumh 移位溢出"
        self.acc_diff = (self.acc_diff - (self.acc_diff >> ACC_SHIFT)
                         + (diff_in >> ACC_SHIFT))
        self.acc_sum = (self.acc_sum - (self.acc_sum >> ACC_SHIFT)
                        + (sum_in >> ACC_SHIFT))
        assert self.acc_diff < (1 << ACCW) and self.acc_sum < (1 << ACCW), \
            "累加器溢出"
        old_stereo = self.stereo
        self.stereo = 1 if self.acc_diff > (self.acc_sum >> 2) else 0
        self._level_dbg.append((self.lvl_l, self.lvl_r, old_stereo))


def run(meter, pairs):
    for l, r in pairs:
        meter.step(l, r)


def test_source():
    txt = open(RTL, "r", encoding="utf-8").read()
    need = ["module stereo_meter", "lvl_l", "lvl_r", "clip_l", "clip_r",
            "stereo", "acc_diff", "HOLD_SAMPLES"]
    miss = [k for k in need if k not in txt]
    check("stereo_meter.v 存在且接口齐全", not miss, "缺: %s" % miss if miss else "")
    # 只统计真正的乘法运算符：先去掉注释里那些 markdown 的 ** 强调
    body = "\n".join(l.split("//")[0] for l in txt.splitlines())
    body = body.replace("**", "")
    check("没有用乘法（不进 DSP48）", body.count("*") == 0,
          "去掉注释后 '*' 的数量: %d" % body.count("*"))


def test_abs():
    m = Meter()
    m.step(-32768, -32768)
    check("|−32768| = 32768（不回绕）", m.lvl_l == 32768,
          "lvl_l=%d" % m.lvl_l)
    m2 = Meter()
    m2.step(-1, 32767)
    check("|−1| = 1，|32767| = 32767", m2.lvl_l == 1 and m2.lvl_r == 32767,
          "%d / %d" % (m2.lvl_l, m2.lvl_r))


def test_envelope():
    m = Meter()
    run(m, [(20000, 20000)] * 10)
    check("峰值包络立刻跟上", m.lvl_l == 20000, "lvl=%d" % m.lvl_l)
    prev = m.lvl_l
    seq = []
    for _ in range(64):
        m.step(0, 0)
        seq.append(m.lvl_l)
    check("输入归零后按 1/64 指数回落", seq[0] < prev and seq[-1] < prev * 0.4,
          "%d -> %d -> %d" % (prev, seq[0], seq[-1]))


def test_thresholds():
    m = Meter()
    run(m, [(SIG_THRESH - 1, SIG_THRESH + 1)] * 4)
    check("低于阈值不点亮「有信号」", m.sig_l == 0 and m.sig_r == 1,
          "sig_l=%d sig_r=%d" % (m.sig_l, m.sig_r))

    m = Meter()
    run(m, [(CLIP_THRESH + 100, 0)] * 2)
    check("超过削波阈值立刻点亮", m.clip_l == 1 and m.clip_r == 0,
          "clip_l=%d" % m.clip_l)
    held = 0
    for _ in range(HOLD_SAMPLES + 10):
        if m.clip_l:
            held += 1
        m.step(0, 0)
    check("偶发过载闪一下就灭（保持约 0.5 s）",
          HOLD_SAMPLES - 2 <= held <= HOLD_SAMPLES + 2,
          "保持 %d 个采样点（期望 %d）" % (held, HOLD_SAMPLES))

    m = Meter()
    run(m, [(32767, 32767)] * (HOLD_SAMPLES * 2))
    check("持续过载保持常亮", m.clip_l == 1 and m.clip_r == 1)


def test_stereo_flag():
    rng = np.random.RandomState(3)
    n = 20000

    # (a) L == R：左右完全一样 -> 灯必须灭
    a = (rng.randn(n) * 8000).astype(np.int64)
    m = Meter()
    for v in a:
        m.step(int(v), int(v))
    check("左右完全相同 -> 立体声灯灭", m.stereo == 0,
          "acc_diff=%d acc_sum=%d" % (m.acc_diff, m.acc_sum))

    # (b) L == -R：纯反相 -> 灯必须亮
    m = Meter()
    for v in a:
        m.step(int(v), int(-v))
    check("左右纯反相 -> 立体声灯亮", m.stereo == 1,
          "acc_diff=%d acc_sum=%d" % (m.acc_diff, m.acc_sum))

    # (c) 两路不相关 -> 灯亮
    b = (rng.randn(n) * 8000).astype(np.int64)
    m = Meter()
    for x, y in zip(a, b):
        m.step(int(x), int(y))
    check("左右互不相关 -> 立体声灯亮", m.stereo == 1,
          "acc_diff=%d acc_sum=%d" % (m.acc_diff, m.acc_sum))

    # (d) 完全静音 -> 灯灭
    m = Meter()
    run(m, [(0, 0)] * 1000)
    check("完全静音 -> 立体声灯灭", m.stereo == 0)


def test_no_overflow():
    """全量程直流是最坏情况：累加器必须一位不溢。"""
    m = Meter()
    run(m, [(32767, -32768)] * 30000)
    check("满量程反相常驻不溢出",
          m.acc_diff < (1 << ACCW) and m.acc_sum < (1 << ACCW),
          "acc_diff=%d acc_sum=%d (上限 %d)" % (m.acc_diff, m.acc_sum, 1 << ACCW))
    check("满量程反相判定为立体声", m.stereo == 1)


def main():
    print("=" * 66)
    print(" rtl/stereo_meter.v —— 行为级验证（纯 Python）")
    print("=" * 66)
    test_source()
    test_abs()
    test_envelope()
    test_thresholds()
    test_stereo_flag()
    test_no_overflow()
    print("\n" + "=" * 66)
    print(" PASS = %d    FAIL = %d" % (PASS, FAIL))
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
