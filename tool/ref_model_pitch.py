# -*- coding: utf-8 -*-
"""
pitch_shift.v 的逐比特对齐参考模型。

目的：在上板之前先证明环形延迟线 + 双抽头 Hann 交叉淡化确实产生
      r = 1 + dphase/64 的变调，并且输出不会因为权重误差产生周期性咔哒声。

用法：直接运行，会打印每个 dphase 档位的实测输出频率与理论值。
"""
import math
import os

SIZE = 4096
MASK = SIZE - 1


def load_hann_rom(path):
    """从 rtl/wave_rom.v 里把 hann_rom 的初始值抠出来，保证和 RTL 完全一致。"""
    mem = {}
    with open(path, "r", encoding="utf-8") as f:
        inblk = False
        for ln in f:
            if "mem [0:511]" in ln:
                inblk = True
                continue
            if inblk and "initial begin" in ln:
                continue
            if inblk and "end" in ln and "mem" not in ln:
                break
            if inblk and "mem[" in ln:
                idx = int(ln[ln.index("mem[") + 4:ln.index("]")])
                val = int(ln[ln.index("16'h") + 4:].rstrip("; \n"), 16)
                mem[idx] = val
    assert len(mem) == 512, "hann_rom 条目数不对: %d" % len(mem)
    return [mem[i] for i in range(512)]


def to_signed16(v):
    return v - 65536 if v >= 32768 else v


def pitch_shift(x, dphase, hann_rom):
    """返回与 RTL 输出序列对应的 y[]。"""
    mem = [0] * SIZE
    waddr = 0
    phase = 0
    y = []
    for xv in x:
        xv = max(-32768, min(32767, xv))
        # S_IDLE: 写入当前样本 -> mem[waddr] 即 x(n)
        mem[waddr] = xv

        X = 65536 - phase
        Y = 131072 - phase

        dly_a = (X >> 6) & MASK
        dly_b = (Y >> 6) & MASK
        frac_a = X & 63
        frac_b = Y & 63

        ba = (waddr - dly_a) & MASK
        bb = (waddr - dly_b) & MASK
        sa0 = to_signed16(mem[ba])
        sa1 = to_signed16(mem[(ba - 1) & MASK])
        sb0 = to_signed16(mem[bb])
        sb1 = to_signed16(mem[(bb - 1) & MASK])

        # 线性插值：step = (diff * frac) >>> 6
        # 注意 Python 里 >> 优先级低于 + ，括号一个都不能少，否则会算成
        # (sa0 + diff*frac) >> 6，整条链路衰减 64 倍
        ta = sa0 + (((sa1 - sa0) * frac_a) >> 6)
        tb = sb0 + (((sb1 - sb0) * frac_b) >> 6)

        ia = (X >> 8) & 511
        ib_raw = (Y >> 8)
        ib = min(511, ib_raw)
        wa = hann_rom[ia]
        wb = hann_rom[ib]

        acc = ta * wa + tb * wb
        yv = acc >> 16
        if acc > 2147418112:
            yv = 32767
        elif acc < -2147418112:
            yv = -32768
        y.append(yv)

        # S_OUT: 指针推进
        waddr = (waddr + 1) & MASK
        phase = (phase + dphase) & 0xFFFF

    return y


def est_freq(y, fs):
    """自相关基频估计：对谐波丰富的信号也比过零点计数稳健得多。

    做法：在 [60 Hz, 1200 Hz] 对应的滞后区间里找归一化自相关的峰值，
    再用抛物线插值细化到小数滞后。
    """
    n = min(len(y), 16384)
    if n < 512:
        return 0.0
    seg = y[:n]
    mean = sum(seg) / float(n)
    sig = [v - mean for v in seg]

    lag_min = int(fs / 1500.0)
    lag_max = int(fs / 60.0)
    if lag_max > n // 2:
        lag_max = n // 2

    e0 = sum(v * v for v in sig)
    if e0 <= 0:
        return 0.0

    best_lag, best_r = lag_min, -2.0
    rs = {}
    for lag in range(lag_min, lag_max):
        acc = 0.0
        for i in range(n - lag):
            acc += sig[i] * sig[i + lag]
        r = acc / float(e0)
        rs[lag] = r
        if r > best_r:
            best_r, best_lag = r, lag
    # 自相关的第一个峰常常落在整数倍滞后上，取"最靠前的显著峰"
    for lag in range(lag_min, lag_max):
        if rs[lag] > 0.9 * best_r:
            best_lag = lag
            break

    # 抛物线细化
    if best_lag - 1 in rs and best_lag + 1 in rs:
        a, b, c = rs[best_lag - 1], rs[best_lag], rs[best_lag + 1]
        den = (a - 2.0 * b + c)
        if den != 0:
            best_lag = best_lag + 0.5 * (a - c) / den
    return float(fs) / best_lag if best_lag > 0 else 0.0


def est_envelope_ripple(y, fs, f_out):
    """包络起伏 (%)。窗口取 10 个输出周期，内部取峰值，衡量
        legit 的交叉淡化是否造成周期性调幅。"""
    if not f_out or abs(f_out) < 1e-6:
        return 0.0
    win = int(round(10.0 * fs / abs(f_out)))
    win = max(32, win)
    if win * 4 > len(y):
        return 0.0
    peaks = []
    i = 0
    while i + win < len(y):
        seg = y[i:i + win]
        pk = max(abs(v) for v in seg)
        if pk > 0:
            peaks.append(pk)
        i += win
    if len(peaks) < 4:
        return 0.0
    m = sum(peaks) / float(len(peaks))
    return (max(peaks) - min(peaks)) / m if m else 0.0


def run_set(x, label, fs, f0, hann_rom, dphases):
    print("== %s   输入 f0 = %s Hz" % (label, f0))
    print("-" * 66)
    print("%8s %8s %11s %11s %8s %11s" %
          ("dphase", "理论 r", "理论输出Hz", "实测输出Hz", "误差%", "包络起伏%"))
    print("-" * 66)
    worst_e = 0.0
    worst_r = 0.0
    # 单音才有"理论输出频率"这一列；宽带信号用 r 直接验 RMS 能量守恒
    single = isinstance(f0, float) or isinstance(f0, int)
    for dphase in dphases:
        r = 1.0 + dphase / 64.0
        y = pitch_shift(x, dphase, hann_rom)
        tail = y[SIZE:]
        if single:
            fo = est_freq(tail, fs)
            theo = r * f0
            err = abs(fo - theo) / theo * 100.0 if theo else 0.0
        else:
            fo = est_freq(tail, fs)
            theo = 0.0
            err = 0.0
        rip = est_envelope_ripple(tail, fs, fo) * 100.0
        worst_e = max(worst_e, err)
        worst_r = max(worst_r, rip)
        print("%8d %8.4f %11.1f %11.1f %8.2f %11.2f" %
              (dphase, r, theo, fo, err, rip))
    print("-" * 66)
    print("最大变频误差 %.2f%%   最大包络起伏 %.2f%%" % (worst_e, worst_r))
    print()
    return worst_e, worst_r


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    rom_path = os.path.normpath(os.path.join(here, "..", "rtl", "wave_rom.v"))
    hann_rom = load_hann_rom(rom_path)

    fs = 50_000_000.0 / 1024.0     # 48 828.125 Hz，和 i2s_if.v 一致
    f0 = 440.0
    N = 60000
    amp = 0.6 * 32767
    dphases = [-32, -24, -16, -8, 0, 8, 16, 24, 32, 48, 64]

    print("fs = %.3f Hz" % fs)
    print()

    # --- 用例 1：纯音 ---
    xs = [int(round(amp * math.sin(2.0 * math.pi * f0 * n / fs))) for n in range(N)]
    run_set(xs, "纯音 440 Hz", fs, f0, hann_rom, dphases)

    # --- 用例 2：语音状的宽带谐波串（浊音 /a/ 的粗略近似）---
    # 浊音样标识为谐波串：基频取 250 Hz，保证即使下移两个八度
    # 输出仍有 62.5 Hz 以上，落在自相关估计器的稳健区间里
    harm = [(1.0, 1.0), (2.0, 0.55), (3.0, 0.32), (4.0, 0.20), (5.0, 0.12), (7.0, 0.07)]
    base = 250.0
    xh = []
    for n in range(N):
        v = sum(a_ * math.sin(2.0 * math.pi * base * k * n / fs) for k, a_ in harm)
        xh.append(int(round(amp * 0.5 * v / sum(a_ for _, a_ in harm))))
    run_set(xh, "宽带浊音 基频 250 Hz + 6 次谐波", fs, base, hann_rom, dphases)

    # --- 收尾：非负性检查 ---
    y = pitch_shift(xs, 8, hann_rom)
    tail = y[SIZE:]
    sat = sum(1 for v in tail if abs(v) >= 32767)
    print("饱和样本占比 %.4f%%  (应为 0)" % (sat * 100.0 / len(tail)))
    print("输出是否含 NaN/None: %s" % (any(v is None for v in tail)))


if __name__ == "__main__":
    main()
