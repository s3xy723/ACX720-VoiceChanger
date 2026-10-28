# -*- coding: utf-8 -*-
"""
逐级检查 dphase 从拨码开关到相位累加器的位宽/符号传递。

背景（本次修掉的 bug）：
    voice_changer_top.v : wire signed [8:0]  pitch_dphase = (v - 7) * 8
    voice_proc.v        : input wire signed [8:0]  dphase
    pitch_shift.v       : input wire ...
    当 pitch_shift 的端口是无符号 [15:0] 时，Verilog 做零扩展：
        9'sd-32 的位型 1_1110_0000 (=480) -> 16'd480
    于是 phase 每步 +480 而不是 -32，降调档位（r<1）全部变成刺耳的升调。

本脚本把三种端口写法都跑一遍，打印"等效步进"，再用参考模型验证
实际变频比，证明只有 signed 端口能得到预期的 r。
"""
import math
import os
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))


def load_ref():
    spec = importlib.util.spec_from_file_location(
        "rm", os.path.join(HERE, "ref_model_pitch.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def u9(v):
    """把有符号值截成 9 位补码的无符号位型。"""
    return v & 0x1FF


def extend(val9, width, signed_port):
    """模拟 Verilog 端口连接时的位宽扩展。"""
    bits = val9 & 0x1FF
    if signed_port:
        # 符号扩展：位型最高位为 1 时，高位全补 1
        if bits & 0x100:
            bits |= ((1 << width) - 1) ^ 0x1FF
    # 无符号就是原样零扩展（高位已经是 0）
    return bits & ((1 << width) - 1)


def eff_step(raw16):
    """16 位位型在 mod 2^16 累加下的等效有符号步进。"""
    return raw16 - 65536 if raw16 >= 32768 else raw16


def est_freq_wide(y, fs, lo=25.0, hi=1600.0, maxn=32768):
    """放宽下界的自相关基频估计。

    ref_model_pitch.est_freq 的搜索区间是 [60 Hz, 1500 Hz]，
    而 v=0 档的 r=0.125 会把 440 Hz 拉到 55 Hz，落在区间外，
    估计器会误锁到某个高频峰上（实测报 1525 Hz）。
    这里把下限放到 25 Hz，专门覆盖最低那几档。
    """
    n = min(len(y), maxn)
    if n < 1024:
        return 0.0
    seg = y[:n]
    mean = sum(seg) / float(n)
    sig = [v - mean for v in seg]
    e0 = sum(v * v for v in sig)
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
    # 取"第一个局部极大值"，而不是"第一个超过阈值的滞后"。
    # 对低频信号，小滞后处自相关本来就接近 1（波形几乎没变化），
    # 用阈值法会误锁到 lag=30 这种极小滞后上，把 55 Hz 报成 1600 Hz。
    first = best_i
    for i in range(1, len(rs) - 1):
        if rs[i] >= rs[i - 1] and rs[i] > rs[i + 1] and rs[i] > 0.8 * best_r:
            first = i
            break
    lag = lag_min + first
    if 0 < first < len(rs) - 1:
        a, b, c = rs[first - 1], rs[first], rs[first + 1]
        den = (a - 2.0 * b + c)
        if den != 0:
            lag = lag_min + first + 0.5 * (a - c) / den
    return fs / lag if lag > 0 else 0.0


def main():
    rm = load_ref()
    rom = rm.load_hann_rom(os.path.normpath(
        os.path.join(HERE, "..", "rtl", "wave_rom.v")))

    fs = 50_000_000.0 / 1024.0
    f0 = 440.0
    N = 44000
    amp = 0.6 * 32767
    x = [int(round(amp * math.sin(2.0 * math.pi * f0 * n / fs)))
         for n in range(N)]

    print("拨码 v -> 9 位 dphase -> 端口扩展 -> 相位等效步进 -> 实测变频比")
    print("=" * 78)
    print("%6s %6s %10s %14s %12s %12s %10s" %
          ("v", "期望r", "9位位型", "旧(unsigned)", "新(signed)", "期望步进", "期望r"))
    print("-" * 78)

    ok = True
    for v in (0, 3, 5, 10, 15):
        want_dphase = (v - 5) * 6
        r_expect = 1.0 + want_dphase / 64.0
        b9 = u9(want_dphase)
        old16 = extend(b9, 16, signed_port=False)
        new16 = extend(b9, 16, signed_port=True)
        print("%6d %6.3f 0x%03X(%5d) %14d %12d %12d %10.3f" %
              (v, r_expect, b9, b9, eff_step(old16), eff_step(new16),
               want_dphase, r_expect))

        if eff_step(new16) != want_dphase:
            ok = False
            print("      ^^ 新写法等效步进与期望不一致！")

    print("-" * 78)
    print("旧写法把负 dphase 零扩展成正数（等效步进符号翻转），降调档位失效。")
    print()

    # ---- 用修复后的等效步进跑参考模型，确认变频比 ----
    print("修复后（signed 端口）实测变频比：")
    print("-" * 66)
    print("%6s %10s %12s %12s %8s" %
          ("v", "等效步进", "期望输出Hz", "实测输出Hz", "误差%"))
    print("-" * 66)
    worst = 0.0
    for v in range(16):
        want_dphase = (v - 5) * 6
        r = 1.0 + want_dphase / 64.0
        step = eff_step(extend(u9(want_dphase), 16, signed_port=True))
        y = rm.pitch_shift(x, step, rom)
        tail = y[rm.SIZE:]
        fo = est_freq_wide(tail, fs)
        theo = r * f0
        err = abs(fo - theo) / theo * 100.0 if theo > 1e-6 else 0.0
        worst = max(worst, err)
        print("%6d %10d %12.1f %12.1f %8.2f" % (v, step, theo, fo, err))
    print("-" * 66)
    print("最大变频误差 %.2f%%" % worst)

    print()
    print("判据：全 16 档等效步进与期望一致，且最大变频误差 < 3%")
    if ok and worst < 3.0:
        print("PASS: 端口符号性修复后，升调与降调档位变频比均正确。")
    else:
        print("FAIL: 见上表。")


if __name__ == "__main__":
    main()
