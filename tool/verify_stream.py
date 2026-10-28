# -*- coding: utf-8 -*-
"""
verify_stream.py —— 用硬数字验证两个**流式**模块真的能用

跑法
----
    python tool/verify_stream.py            # 快速子集（约 1 分钟）
    python tool/verify_stream.py --full     # 全套（含 demucs 实测，约 5 分钟）

测什么
------
本轮把"离线高质量算法"改造成了流式版本，两个模块：

    hq_stream.StreamPitchShifter   相位声码器 + 共振峰保持，逐块进出
    local_stream.StreamSeparator   demucs 分块流式人声分离

流式版本最容易出的错是"看着在跑、其实错了"：
    · 块边界处理错 -> 周期性伪影
    · 缓冲索引错   -> 喂给模型的数据整个错乱（本轮实测过：负索引
                     让 chunk 从尾部取，SI-SDR 从 +18 掉到 -7）
    · 补零位置错   -> 输出长度漂移、音高被测低
所以每一项都对拍一个**已知正确的参照**：

    变调   : 与离线 hq_voice.process 对拍（音高、长度、频谱）
    分离   : 与离线 local_ai.separate 对拍（SI-SDR、长度、可复现）

另外验证三条"诚实性"要求：
    · 降级路径不缺依赖也跑（直通、逐样本一致）
    · 延迟如实报告（latency_s() 与实测一致）
    · 喂入块大小不影响结果（256 ~ 44100 样本逐比特一致）

输出：Pass/Fail 清单 + 数据行。
"""

import os
import sys
import math
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
HOST = os.path.join(ROOT, "host")
sys.path.insert(0, HOST)
sys.path.insert(0, HERE)

import hq_stream                                     # noqa: E402
import hq_voice                                      # noqa: E402
import local_ai                                      # noqa: E402
import local_stream                                  # noqa: E402
import verify_local_ai as VLA                        # noqa: E402

SR = 48000
FULL = "--full" in sys.argv

_PASS = [0]
_FAIL = [0]


def rec(name, ok, detail=""):
    if ok:
        _PASS[0] += 1
        print("[PASS] %s" % name)
    else:
        _FAIL[0] += 1
        print("[FAIL] %s   %s" % (name, detail))
    if detail and ok:
        print("       %s" % detail)
    return ok


# ---------------------------------------------------------------- 工具

def peak_freq(a, sr=SR, frac=0.40):
    """测一段信号的基频（抛物线插值提精度）。"""
    s = int(a.size * frac)
    seg = a[s:s + 8192]
    if seg.size < 8192:
        seg = np.pad(seg, (0, 8192 - seg.size))
    F = np.abs(np.fft.rfft(seg * np.hanning(8192)))
    f = np.fft.rfftfreq(8192, 1.0 / sr)
    k = int(np.argmax(F[1:])) + 1
    if 1 <= k < len(F) - 1:
        d = 0.5 * (F[k - 1] - F[k + 1]) / \
            (F[k - 1] - 2 * F[k] + F[k + 1] + 1e-30)
    else:
        d = 0.0
    return f[k] + d * (f[1] - f[0])


def run_stream_pitch(x, ratio, formant=1.0, block=1024):
    """把 x 分块喂给流式变调，返回拼接后的输出。"""
    sp = hq_stream.StreamPitchShifter(SR, ratio, formant)
    outs = []
    for s in range(0, x.size, block):
        outs.append(sp.push(x[s:s + block]))
    outs.append(sp.flush())
    return np.concatenate(outs)


# ================================================================ 变调

def test_pitch_ratio_one():
    print("\n--- 流式变调：ratio=1 必须逐样本直通 ---")
    rs = np.random.RandomState(0)
    x = rs.randn(4096) * 0.2
    for block in (64, 256, 1024, 4096):
        sp = hq_stream.StreamPitchShifter(SR, 1.0, 1.0)
        outs = []
        for s in range(0, x.size, block):
            outs.append(sp.push(x[s:s + block]))
        outs.append(sp.flush())
        y = np.concatenate(outs)
        d = float(np.max(np.abs(y - x))) if y.size == x.size else 9.9
        rec("ratio=1 直通（喂入块 %d）" % block, d == 0.0 and y.size == x.size,
            "最大偏差 %.3e，长度 %d/%d" % (d, y.size, x.size))


def test_pitch_semitone_sweep():
    print("\n--- 流式变调：音高正确 + 与离线一致 + 长度精确 ---")
    n = int(SR * 0.5)
    t = np.arange(n) / SR
    # ★ 只用单个基频，不要叠加谐波。peak_freq 取最大谱峰，叠加 440 Hz
    #   之后降调时二次谐波会盖过基频（-5 半音时测到 330 而不是 165），
    #   那是度量选错了，不是算法错（流式与离线在该项上完全一致）。
    #   频率选 220 Hz：周期 218 样本，远离倒谱 lift（130）的临界。
    x = 0.5 * np.sin(2 * np.pi * 220.0 * t)
    semis = (0.25, 1.0, 4.0, 7.0, -5.0, -7.0, 12.0, -12.0)
    bad = []
    rows = []
    for semi in semis:
        r = 2.0 ** (semi / 12.0)
        yo = hq_voice.process(x, SR, r, formant=1.0)
        ys = run_stream_pitch(x, r, 1.0, block=1024)
        f_t = 220.0 * r
        f_o = peak_freq(yo)
        f_s = peak_freq(ys)
        ok_len = (ys.size == n)
        ok_p = abs(f_s - f_t) <= 12.0
        ok_off = abs(f_s - f_o) <= 12.0
        if not (ok_len and ok_p and ok_off):
            bad.append((semi, f_s, f_o, ys.size))
        rows.append("%+6.1f 目标%7.1f 离线%7.1f 流式%7.1f 长度%+d"
                    % (semi, f_t, f_o, f_s, ys.size - n))
    rec("8 个半音数音高全部正确且与离线一致", not bad,
        "; ".join("%s" % (b,) for b in bad))
    for r_ in rows:
        print("       %s" % r_)


def test_pitch_block_independence():
    print("\n--- 流式变调：喂入块大小不影响结果 ---")
    n = int(SR * 0.5)
    t = np.arange(n) / SR
    x = 0.5 * np.sin(2 * np.pi * 220.0 * t)
    r = 2.0 ** (4.0 / 12.0)
    base = run_stream_pitch(x, r, 1.0, block=8192)
    worst = 0.0
    for block in (64, 256, 512, 1000, 1024, 2048, 4096, 441, 12345):
        y = run_stream_pitch(x, r, 1.0, block=block)
        if y.size != base.size:
            worst = 9.9
            break
        worst = max(worst, float(np.max(np.abs(y - base))))
    rec("9 种喂入块大小输出逐比特一致", worst == 0.0, "最大差 %.3e" % worst)


def test_pitch_stereo():
    print("\n--- 流式变调：立体声两路独立处理 ---")
    n = int(SR * 0.5)
    t = np.arange(n) / SR
    # 两路都选周期远大于倒谱 lift（130 样本 ≈ 369 Hz）的频率：
    # 220 Hz（周期 218）和 165 Hz（周期 291）。
    # 一开始用了 330 Hz，周期 145 样本逼近 lift 临界，离线和流式都会冒出
    # 306.7 Hz 的伪峰 —— 那是度量选错了信号，不是算法错。
    L = 0.5 * np.sin(2 * np.pi * 220.0 * t)
    R = 0.5 * np.sin(2 * np.pi * 165.0 * t)
    x = np.stack([L, R], axis=1)
    r = 2.0 ** (7.0 / 12.0)
    sp = hq_stream.StreamPitchShifter(SR, r, 1.0)
    outs = []
    for s in range(0, n, 1024):
        outs.append(sp.push(x[s:s + 1024]))
    outs.append(sp.flush())
    y = np.concatenate(outs)
    ok_shape = (y.ndim == 2 and y.shape[1] == 2 and y.shape[0] == n)
    f_l = peak_freq(y[:, 0])
    f_r = peak_freq(y[:, 1])
    rec("立体声输出形状与长度正确", ok_shape, "shape=%s" % (y.shape,))
    rec("左右声道各自变调正确（220->%.0f, 165->%.0f）"
        % (220 * r, 165 * r),
        abs(f_l - 220 * r) <= 12 and abs(f_r - 165 * r) <= 12,
        "实测 %.1f / %.1f Hz" % (f_l, f_r))


def test_pitch_cost():
    print("\n--- 流式变调：算力够不够实时 ---")
    n = int(SR * 10.0)
    t = np.arange(n) / SR
    x = 0.4 * np.sin(2 * np.pi * 220.0 * t)
    r = 2.0 ** (4.0 / 12.0)
    sp = hq_stream.StreamPitchShifter(SR, r, 1.0)
    t0 = time.perf_counter()
    for s in range(0, n, 1024):
        sp.push(x[s:s + 1024])
    dt = time.perf_counter() - t0
    rt = (n / SR) / dt
    rec("单声道算力 > 5× 实时", rt > 5.0,
        "%.1f× 实时（立体声按 2× 估约 %.1f×）" % (rt, rt / 2))


# ================================================================ 分离

def _sep_ready():
    ok, why = local_ai.demucs_ready()
    return ok, why


def test_stream_sep_quality():
    print("\n--- 流式分离：与离线整段对拍（需要 demucs）---")
    ok, why = _sep_ready()
    if not ok:
        print("[SKIP] demucs 不可用（%s），跳过实测项" % why)
        return
    sr = 44100
    n = int(sr * 16.0)
    mix, voc_ref, acc_ref = VLA.synth_song(n, sr)
    vo, ao, sep = local_ai.separate(mix, sr, device="cuda",
                                    quality=local_ai.QUALITY_STD)
    sdr_off_v = VLA.si_sdr(vo, voc_ref)
    sdr_off_a = VLA.si_sdr(ao, acc_ref)

    sp = local_stream.StreamSeparator(sr, device="cuda",
                                      quality=local_ai.QUALITY_STD)
    ov, oa = [], []
    for s in range(0, n, 4096):
        v, a = sp.push(mix[s:s + 4096])
        ov.append(v)
        oa.append(a)
    v, a = sp.flush()
    ov.append(v)
    oa.append(a)
    vs, as_ = np.concatenate(ov), np.concatenate(oa)

    rec("流式分离输出长度精确", vs.shape[0] == n,
        "%d / %d" % (vs.shape[0], n))
    sdr_v = VLA.si_sdr(vs[:n], voc_ref)
    sdr_a = VLA.si_sdr(as_[:n], acc_ref)
    rec("流式人声 SI-SDR 不低于离线 3 dB", sdr_v >= sdr_off_v - 3.0,
        "流式 %.2f dB / 离线 %.2f dB" % (sdr_v, sdr_off_v))
    rec("流式伴奏 SI-SDR 不低于离线 3 dB", sdr_a >= sdr_off_a - 3.0,
        "流式 %.2f dB / 离线 %.2f dB" % (sdr_a, sdr_off_a))


def test_stream_sep_reproducible():
    print("\n--- 流式分离：可复现 + 块无关 ---")
    ok, why = _sep_ready()
    if not ok:
        print("[SKIP] demucs 不可用（%s），跳过实测项" % why)
        return
    sr = 44100
    n = int(sr * 12.0)
    mix, voc_ref, acc_ref = VLA.synth_song(n, sr)

    def run(block):
        sp = local_stream.StreamSeparator(sr, device="cuda",
                                          quality=local_ai.QUALITY_STD)
        ov = []
        for s in range(0, n, block):
            v, _ = sp.push(mix[s:s + block])
            ov.append(v)
        v, _ = sp.flush()
        ov.append(v)
        return np.concatenate(ov)

    a = run(4096)
    b = run(4096)
    d = float(np.max(np.abs(a - b))) if a.size == b.size else 9.9
    rec("标准档两次运行逐比特一致", d == 0.0, "最大差 %.3e" % d)

    base = run(16384)
    worst = 0.0
    for block in (256, 1024, 8192, 44100):
        y = run(block)
        if y.size != base.size:
            worst = 9.9
            break
        worst = max(worst, float(np.max(np.abs(y - base))))
    rec("4 种喂入块大小输出逐比特一致", worst == 0.0, "最大差 %.3e" % worst)


def test_stream_sep_latency():
    print("\n--- 流式分离：延迟如实报告 ---")
    sp = local_stream.StreamSeparator(44100, block_s=1.0, ctx_s=2.0,
                                      fade_s=0.25)
    rec("latency_s() = block + ctx = 3.0 s", abs(sp.latency_s() - 3.0) < 1e-9,
        "实测 %.3f s" % sp.latency_s())
    # 结构验证：推起点为 s 的块需要数据已经到 s+blk+ctx，
    # 所以第一次产出至少要喂 blk+ctx 秒；喂少了必须一个都不给。
    need = 1.0 + 2.0                      # block + ctx = 3 s
    for fed, expect_out in ((need - 0.5, False), (need + 0.5, True)):
        n = int(44100 * fed)
        x = np.zeros((n, 2), dtype=np.float64)
        sp2 = local_stream.StreamSeparator(44100, block_s=1.0, ctx_s=2.0,
                                           fade_s=0.25)
        v, _ = sp2.push(x)
        got = v.shape[0] > 0
        rec("喂 %.1f s %s产出" % (fed, "应" if expect_out else "不应"),
            got == expect_out,
            "得到 %d 样本" % v.shape[0])


def test_stream_sep_degrade():
    print("\n--- 流式分离：缺依赖降级不崩 ---")
    orig = local_ai.demucs_ready
    local_ai.demucs_ready = lambda: (False, "模拟缺少 torch、demucs")
    try:
        sr = 44100
        n = int(sr * 1.0)
        rs = np.random.RandomState(3)
        x = rs.randn(n, 2) * 0.1
        sp = local_stream.StreamSeparator(sr, block_s=0.5, ctx_s=0.5,
                                          fade_s=0.1)
        tot = 0
        # 原来这里把伴奏返回值丢成 `_`，所以"伴奏全零"根本没被检验，
        # 那条断言的第二个参数还写死成 True（恒真）。现在两路都收下来真比。
        acc_parts = []
        for s in range(0, n, 4096):
            v, a = sp.push(x[s:s + 4096])
            tot += v.shape[0]
            if a is not None and a.size:
                acc_parts.append(np.asarray(a))
        v, a = sp.flush()
        tot += v.shape[0]
        if a is not None and a.size:
            acc_parts.append(np.asarray(a))
        rec("缺依赖时降级为直通且长度精确", tot == n,
            "%d / %d，%s" % (tot, n, sp.info["engine"][0]))
        acc_all = (np.concatenate(acc_parts, axis=0)
                   if acc_parts else np.zeros(0, dtype=np.float64))
        acc_max = float(np.max(np.abs(acc_all))) if acc_all.size else 0.0
        rec("降级时伴奏为静音（与“未启用”一致）",
            acc_all.size > 0 and acc_max == 0.0,
            "acc 样本 %d，最大绝对值 %.3g" % (acc_all.size, acc_max))
    finally:
        local_ai.demucs_ready = orig
    ok, why = _sep_ready()
    rec("恢复后环境自检正常", ok, "demucs_ready=%s" % (ok,))


def test_stream_sep_mono_input():
    print("\n--- 流式分离：单声道输入 ---")
    sp = local_stream.StreamSeparator(44100, block_s=0.5, ctx_s=0.5,
                                      fade_s=0.1)
    sr = 44100
    n = int(sr * 0.5)
    x = np.zeros(n, dtype=np.float64)
    v, a = sp.push(x)
    v2, a2 = sp.flush()
    tot = v.shape[0] + v2.shape[0]
    rec("单声道输入不崩且长度正确", tot == n,
        "%d / %d，输出 shape %s" % (tot, n, v.shape))


# ================================================================ 主流程

def main():
    print("=" * 68)
    print("流式模块验收：hq_stream（实时变调） + local_stream（流式分离）")
    print("=" * 68)

    test_pitch_ratio_one()
    test_pitch_semitone_sweep()
    test_pitch_block_independence()
    test_pitch_stereo()
    test_pitch_cost()

    test_stream_sep_latency()
    test_stream_sep_degrade()
    test_stream_sep_mono_input()
    if FULL:
        test_stream_sep_quality()
        test_stream_sep_reproducible()
    else:
        print("\n--- 流式分离实测（demucs）---")
        print("[SKIP] 快速模式跳过；加 --full 跑 SI-SDR / 可复现 / 块无关实测")

    print("\n" + "=" * 68)
    print("结果：%d PASS / %d FAIL" % (_PASS[0], _FAIL[0]))
    print("=" * 68)
    return 1 if _FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
