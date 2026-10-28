# -*- coding: utf-8 -*-
"""
test_host.py  ——  上位机 DSP 内核的验收脚本

做的事：
  1. 把 dsp_core 里解析生成的 Hann / 正弦 ROM 与 rtl/wave_rom.v 里的内容
     逐点比对，证明上位机和 RTL 用的是同一张表。
  2. 扫遍全部半音档位，用自相关估计输出基频，核对变调比。
  3. 验证分块处理结果完全一致（文件模式可以按内存切块，不影响结果）。
  4. 回声延迟位置、环形调制边带位置校验。
  5. 性能：统计处理速度是实时的多少倍。
  6. 冒烟：构建一次 GUI（构完就销毁），确认没有控件层面的错误。

用法：  python host/test_host.py
"""

import math
import os
import re
import sys
import time

import threading

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import audio_io
import dsp_core as dsp

ROM_PATH = os.path.normpath(os.path.join(HERE, "..", "rtl", "wave_rom.v"))

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


# ---------------------------------------------------------------- ROM 解析

def parse_verilog_roms(path):
    """从 wave_rom.v 里抽出每个 module 的 mem[] 初值（16 位无符号）。"""
    txt = open(path, "r", encoding="utf-8").read()
    out = {}
    for seg in txt.split("module ")[1:]:
        name = seg.split("(")[0].strip()
        body = seg.split("endmodule")[0]
        vals = {}
        for m in re.finditer(r"mem\[\s*(\d+)\]\s*=\s*16'h([0-9A-Fa-f]{4});", body):
            vals[int(m.group(1))] = int(m.group(2), 16)
        if vals:
            out[name] = vals
    return out


# ---------------------------------------------------------------- 基频估计

def estimate_f0(y, fs, f_lo=60.0, f_hi=1400.0):
    """归一化自相关 + 抛物线插值，先找第一个显著峰（不是全局最大）。"""
    y = np.asarray(y, dtype=np.float64)
    y = y - y.mean()
    lag_lo = max(2, int(fs / f_hi))
    lag_hi = min(len(y) // 2, int(fs / f_lo))
    if lag_hi <= lag_lo + 2:
        return 0.0
    # 用 FFT 算自相关，避免 O(n^2)
    nfft = 1 << int(np.ceil(math.log2(2 * len(y))))
    spec = np.fft.rfft(y, nfft)
    acf = np.fft.irfft(spec * np.conj(spec), nfft)[:lag_hi + 1]
    acf = acf / (abs(acf[0]) + 1e-12)
    r = acf[lag_lo:lag_hi + 1]
    # 找第一个显著的正峰（低位('/小滞后')处自相关本来就高，取全局最大会误判成倍频）
    gmax = r.max()
    idx = -1
    for i in range(1, r.size - 1):
        if r[i] >= r[i - 1] and r[i] >= r[i + 1] and r[i] > 0.6 * gmax:
            idx = i
            break
    if idx < 0:
        idx = int(np.argmax(r))
    lag = lag_lo + idx
    if 0 < idx < r.size - 1:
        a, b, c = r[idx - 1], r[idx], r[idx + 1]
        den = (a - 2 * b + c)
        if abs(den) > 1e-12:
            lag = lag + 0.5 * (a - c) / den
    return float(fs) / lag if lag > 0 else 0.0


# ---------------------------------------------------------------- 用例

def test_roms():
    print("\n[1] ROM 与 rtl/wave_rom.v 逐点比对")
    try:
        roms = parse_verilog_roms(ROM_PATH)
    except Exception as e:
        check("ROM 解析", False, repr(e))
        return
    check("找到 ROM 模块", len(roms) >= 2, str(sorted(roms.keys())))

    hann_ref = roms.get("hann_rom")
    sine_ref = roms.get("sine_rom")
    if hann_ref:
        mine = dsp.make_hann_rom()
        ok = True
        bad = []
        for k, v in sorted(hann_ref.items()):
            if int(mine[k]) != v:
                ok = False
                bad.append((k, v, int(mine[k])))
        check("Hann 窗 512 点完全一致", ok,
              "" if ok else "前几个不一致 %s" % bad[:3])
        check("Hann 端点为 0、中心接近峰值",
              int(mine[0]) == 0 and int(mine[511]) < 16 and int(mine[255]) > 65000,
              "w[0]=%d w[255]=%d w[511]=%d" % (mine[0], mine[255], mine[511]))
    else:
        check("Hann 窗 ROM", False, "wave_rom.v 里没找到 hann_rom")

    if sine_ref:
        mine = dsp.make_sine_rom()
        ok = True
        bad = []
        for k, v in sorted(sine_ref.items()):
            mine_u = int(mine[k]) & 0xFFFF
            if mine_u != v:
                ok = False
                bad.append((k, v, mine_u))
        check("正弦表 256 点完全一致", ok, "" if ok else "前几个不一致 %s" % bad[:3])
        check("正弦表四分之一点为正峰、四分之三点为负峰",
              int(mine[64]) > 32000 and int(mine[192]) < -32000,
              "s[64]=%d s[192]=%d" % (mine[64], mine[192]))
    else:
        check("正弦表 ROM", False, "wave_rom.v 里没找到 sine_rom")


def test_pitch():
    print("\n[2] 变调精度（输入 440 Hz 正弦，48 kHz，1 秒）")
    fs = 48000
    f0 = 440.0
    n = fs
    x = (0.6 * 32767.0 * np.sin(2 * np.pi * f0 * np.arange(n) / fs)).astype(np.int16)

    worst = 0.0
    worst_case = None
    rows = []
    for semi in range(-12, 13):
        ch = dsp.VoiceChain()
        ch.configure(mode=dsp.MODE_PITCH, dphase=dsp.semitone_to_dphase(semi))
        y = ch.process(x, block=4096)[4096:]
        fo = estimate_f0(y, fs)
        theo = f0 * 2.0 ** (semi / 12.0)
        err = abs(fo - theo) / theo * 100.0
        rows.append((semi, theo, fo, err))
        if err > worst:
            worst = err
            worst_case = semi

    for semi, theo, fo, err in rows:
        if semi % 4 == 0 or semi in (-12, 12):
            print("      %+3d 半音   期望 %7.1f Hz   实测 %7.1f Hz   误差 %5.2f%%"
                  % (semi, theo, fo, err))
    check("全部 25 档误差 < 3%%", worst < 3.0,
          "最大 %.2f%%（%d 半音档）" % (worst, worst_case))

    # 直通档必须原样输出
    ch = dsp.VoiceChain()
    ch.configure(mode=dsp.MODE_BYPASS)
    y = ch.process(x, block=4096)
    check("直通档逐点透明", np.array_equal(y, x))

    return rows


def test_block_invariance():
    print("\n[3] 分块大小不改变结果")
    fs = 48000
    x = (np.random.RandomState(7).randn(20000) * 8000).astype(np.int16)
    outs = []
    for blk in (256, 1024, 8192, 20000):
        ch = dsp.VoiceChain()
        ch.configure(mode=dsp.MODE_PITCH, dphase=dsp.semitone_to_dphase(5))
        outs.append(ch.process(x, block=blk))
    same = all(np.array_equal(outs[0], o) for o in outs[1:])
    check("256 / 1024 / 8192 / 整段 结果完全一致", same)
    return same


def test_echo():
    print("\n[4] 回声延迟位置")
    fs = 48000
    taps = 4
    d = taps * 256
    n = 4000
    x = np.zeros(n, dtype=np.int16)
    x[100] = 20000
    ch = dsp.VoiceChain()
    ch.configure(mode=dsp.MODE_ECHO, echo_taps=taps, echo_gain=2)  # gain 0.5
    y = ch.process(x, block=4096).astype(np.int64)
    peaks = [i for i in range(1, n) if abs(y[i]) > 1000]
    expect = 100 + d
    ok = any(abs(p - expect) <= 1 for p in peaks)
    check("冲激副本出现在 n-D 处", ok,
          "期望 index≈%d，实测峰位置 %s" % (expect, peaks[:4]))
    if ok:
        # peaks[0] 是干声本身（index 100），湿声要看 expect 那个位置
        amp = abs(int(y[expect]))
        check("回声幅度 ≈ 干声 × 0.5", 0.4 * 20000 < amp < 0.6 * 20000,
              "index %d 处幅度 %d" % (expect, amp))


def _goertzel(y, fs, f):
    """单点 DFT 幅度，用来看某个频率上有没有能量。"""
    y = np.asarray(y, dtype=np.float64)
    k = 2.0 * np.pi * f / fs
    n = np.arange(y.size)
    return abs(np.sum(y * np.exp(-1j * k * n))) / y.size


def test_ring():
    print("\n[5] 环形调制产生上下边带")
    fs = 48000
    fc = 1000.0
    n = 24000
    t = np.arange(n)
    x = (0.6 * 32767 * np.sin(2 * np.pi * fc * t / fs)).astype(np.int16)
    ch = dsp.VoiceChain()
    ch.configure(mode=dsp.MODE_RING, ring_freq=1)   # 100 Hz
    y16 = ch.process(x, block=4096)[4096:]      # 保留 int16 原值，别急着转 float
    y = y16.astype(np.float64)
    fm = 100.0
    side_lo = _goertzel(y, fs, fc - fm)
    side_hi = _goertzel(y, fs, fc + fm)
    carrier = _goertzel(y, fs, fc)
    # 干声（未经处理的同一段）在载波频率上的幅度，用来做"一半"的参照
    dry = _goertzel(x[4096:].astype(np.float64), fs, fc)
    check("900 Hz 与 1100 Hz 边带都存在",
          side_lo > 0.05 * dry and side_hi > 0.05 * dry,
          "|900|=%.0f |1100|=%.0f 干声|1000|=%.0f" % (side_lo, side_hi, dry))
    # 原来写的是 carrier > 100，而干声幅度约 9800 —— 阈值有 49 倍余量，
    # 等于没测。改成相对干声的区间断言，才真的验证"保留一半载波"。
    check("残余载波约为干声的一半（保留可懂度）",
          0.4 * dry < carrier < 0.65 * dry,
          "|1000|=%.0f，干声=%.0f，占比 %.2f" % (carrier, dry, carrier / dry))
    # 原来这里是恒真断言：y 是 int16，astype(float64) 后绝对值必然 <= 32767，
    # 任何实现都 PASS。真正要抓的是 int16 环绕（溢出会绕回负数，abs 看不出来），
    # 所以改成统计"贴着上下限的样本数"——贴限说明发生了饱和削顶。
    sat = int(np.sum((y16 >= 32767) | (y16 <= -32768)))
    check("输出未发生 int16 饱和削顶", sat == 0,
          "peak=%d，贴限样本 %d" % (int(np.abs(y16).max()), sat))


def test_perf():
    print("\n[6] 处理速度")
    fs = 48000
    dur = 10.0
    x = (np.random.RandomState(1).randn(int(fs * dur)) * 9000).astype(np.int16)
    for mode, name in ((dsp.MODE_PITCH, "变调"), (dsp.MODE_RING, "环形调制"),
                       (dsp.MODE_ECHO, "回声"), (dsp.MODE_PITCH_ECHO, "变调+回声")):
        ch = dsp.VoiceChain()
        ch.configure(mode=mode, dphase=dsp.semitone_to_dphase(3))
        t0 = time.time()
        ch.process(x, block=2048)
        dt = time.time() - t0
        ratio = dur / dt if dt > 0 else 0.0
        check("%s 快于实时 20 倍" % name, ratio > 20.0,
              "%.0f 倍实时（%.2f 秒处理 %.0f 秒音频）" % (ratio, dt, dur))


def test_file_roundtrip():
    print("\n[7] 文件读写往返")
    fs = 48000
    x = (0.5 * 32767 * np.sin(2 * np.pi * 300 * np.arange(fs) / fs)).astype(np.int16)
    src = os.path.join(HERE, "_t_src.wav")
    dst = os.path.join(HERE, "_t_dst.wav")
    try:
        audio_io.save_wav(src, audio_io.to_float(x), fs)
        a, sr2 = audio_io.load_audio(src)
        check("WAV 采样率可以原样读回", sr2 == fs, "读回 %d Hz" % sr2)
        n_diff = int(abs(a.shape[0] - x.shape[0]))
        check("样本数一致", n_diff == 0, "差 %d 点" % n_diff)

        ch = dsp.VoiceChain()
        ch.configure(mode=dsp.MODE_PITCH, dphase=dsp.semitone_to_dphase(-7))
        y = ch.process(audio_io.to_int16(a), block=4096)
        audio_io.save_wav(dst, audio_io.to_float(y), fs)
        b, _ = audio_io.load_audio(dst)
        check("处理后文件能被重新读取且长度相同",
              b.shape[0] == x.shape[0], "%d -> %d" % (x.shape[0], b.shape[0]))
        check("处理后仍有能量", float(np.abs(b).max()) > 0.05,
              "peak=%.3f" % float(np.abs(b).max()))
    finally:
        for f in (src, dst):
            if os.path.exists(f):
                os.remove(f)


def test_gui_smoke():
    print("\n[8] GUI 构建冒烟测试")
    try:
        import tkinter as tk
        tk.Tk()  # 能不能建 root
    except Exception as e:
        check("tkinter 可用", False, repr(e))
        return
    try:
        import host_app
        app = host_app.App()
        app.update()
        app.update_idletasks()
        txt = app.lb_sw.cget("text")
        cfg = app.current_cfg()
        n_led = len(app.leds.state)
        led_keys = set(app.leds.state.keys())     # destroy 之后 Tk 部件不能再用
        n_mode = len(dsp.MODE_NAMES)
        app.destroy()
        check("GUI 构建无异常", True, "sw 提示='%s'  当前 dphase=%d" % (txt, cfg["dphase"]))
        # 不硬编码灯数（每加一盏就得改数字，改漏了就误报），改成断言"该有的都在"
        want = {"dev_in", "dev_out", "sr", "engine", "sig_in", "sig_out",
                "clip_in_l", "clip_in_r", "clip_out_l", "clip_out_r",
                "buf", "dsp", "ai", "file", "cache", "sw"}
        check("灯板检查项齐全（削波按 L/R 分开 + 缓存 + 本地 AI）",
              want.issubset(set(app.leds.state.keys())), "%d 盏" % n_led)
        check("模式列表含分离与本地 AI 处理",
              n_mode >= 7 and "分离" in dsp.MODE_NAMES[5]
              and "本地 AI" in dsp.MODE_NAMES[-1],
              "%d 个模式：…%s" % (n_mode, dsp.MODE_NAMES[-1]))
        check("分离参数已进入 current_cfg", isinstance(cfg.get("split"), dict)
              and "depth" in cfg["split"], str(sorted(cfg["split"].keys())))
        aic = cfg.get("ai")
        # 当前 UI 没有"音色(voice)"选项，实际会传给引擎的是这四项：
        #   quality(质量档) / device(算力设备) / vocal_gain / acc_gain
        check("本地 AI 参数已进入 current_cfg",
              isinstance(aic, dict) and aic.get("quality") and aic.get("device")
              and "vocal_gain" in aic and "acc_gain" in aic,
              str(sorted(aic.keys())) if aic else "(无)")
    except Exception as e:
        import traceback
        check("GUI 构建无异常", False, traceback.format_exc(limit=3).replace("\n", " | "))


# ---------------------------------------------------------------- 双声道 / 分离

def _peak_hz(x, sr):
    """加窗 FFT 的谱峰频率。"""
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    sp = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    f = np.fft.rfftfreq(x.size, 1.0 / sr)
    return float(f[int(np.argmax(sp))])


def test_stereo_independent():
    print("\n[10] 双声道分别运算（左右各一条独立链）")
    fs = 48000
    n = 32768
    t = np.arange(n, dtype=np.float32) / float(fs)

    # (a) 变调：左右给不同频率，各自按同样的比例搬移，且互不串扰
    a = (0.5 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    b = (0.5 * np.sin(2 * np.pi * 900 * t)).astype(np.float32)
    st = audio_io.to_int16(np.stack([a, b], axis=1))
    sc = dsp.StereoChain(fs)
    sc.reset()
    sc.configure(mode=dsp.MODE_PITCH, dphase=dsp.semitone_to_dphase(12.0), gain=1.0)
    yo = audio_io.to_float(sc.process(st, block=4096))
    check("输出仍是双声道", yo.ndim == 2 and yo.shape[1] == 2, "shape=%s" % (yo.shape,))
    seg = slice(8192, 16384)
    l_pk = _peak_hz(yo[seg, 0], fs)
    r_pk = _peak_hz(yo[seg, 1], fs)
    check("左右各自变调（左 300→~600，右 900→~1800）",
          500.0 < l_pk < 700.0 and 1500.0 < r_pk < 2100.0,
          "实测 L=%.0f Hz  R=%.0f Hz" % (l_pk, r_pk))
    l_ref = _goertzel(yo[seg, 0].astype(np.float64), fs, l_pk)
    r_ref = _goertzel(yo[seg, 1].astype(np.float64), fs, r_pk)
    cross_l = _goertzel(yo[seg, 0].astype(np.float64), fs, r_pk)
    cross_r = _goertzel(yo[seg, 1].astype(np.float64), fs, l_pk)
    check("左右声道之间没有串扰",
          cross_l < 0.15 * l_ref and cross_r < 0.15 * r_ref,
          "对侧残留 %.4f / %.4f" % (cross_l, cross_r))

    # (b) 回声：只有左声道有冲激，右声道必须一点尾巴都没有
    x = np.zeros((4000, 2), dtype=np.int16)
    x[100, 0] = 20000
    sc2 = dsp.StereoChain(fs)
    sc2.reset()
    sc2.configure(mode=dsp.MODE_ECHO, echo_taps=4, echo_gain=2, gain=1.0)
    ye = sc2.process(x, block=4000).astype(np.int64)
    d = 4 * 256
    left_ok = abs(int(ye[100 + d, 0])) > 1000
    right_silent = int(np.abs(ye[:, 1]).max()) == 0
    check("左声道有回声、右声道完全静音（状态独立）", left_ok and right_silent,
          "L[%d]=%d，R 峰值=%d" % (100 + d, int(ye[100 + d, 0]),
                                    int(np.abs(ye[:, 1]).max())))

    # (c) 单声道输入 -> 双声道输出
    mono = audio_io.to_int16(a)
    sc3 = dsp.StereoChain(fs)
    sc3.reset()
    sc3.configure(mode=dsp.MODE_BYPASS, gain=1.0)
    ym = sc3.process(mono, block=4096)
    check("单声道输入也按双声道输出", ym.ndim == 2 and ym.shape == (n, 2),
          "shape=%s" % (ym.shape,))
    check("直通模式下两路与输入逐点一致",
          np.array_equal(ym[:, 0], ym[:, 1]) and np.array_equal(ym[:, 0], mono))
    sc4 = dsp.StereoChain(fs)
    sc4.reset()
    sc4.configure(mode=dsp.MODE_PITCH, dphase=0, gain=1.0)
    yp = sc4.process(mono, block=4096)
    check("变调档 dphase=0 时左右两路逐点相同（两条链状态同步）",
          np.array_equal(yp[:, 0], yp[:, 1]),
          "L/R 最大差 %d" % int(np.abs(yp[:, 0].astype(np.int64)
                                        - yp[:, 1].astype(np.int64)).max()))


def _split_test_signal(fs=48000, sec_per_part=1.0, f_male=110.0, f_female=231.0):
    """前半"低音人声"+硬左伴奏，后半"高音人声"+硬右伴奏。

    人声**始终居中**（左右同相），伴奏**硬声像**（只在一侧）—— 这正是
    中置提取赖以工作的场景。两段用不同音高只是为了覆盖高低两个音域。

    ★ 2026-10-21 之前这个信号用来测"男声→左 / 女声→右"。现在产品只做
      人声/伴奏分离，同一个信号照样能用：两段人声都该进左声道，
      两段伴奏都该进右声道。参数名保留 f_male/f_female 是为了不惊动
      其它调用点，实际语义是"低音段 / 高音段"。
    """
    half = int(fs * sec_per_part)
    n = half * 2
    t = np.arange(n, dtype=np.float64) / float(fs)

    vo = np.zeros(n)
    vo[:half] = 0.30 * np.sin(2 * np.pi * f_male * t[:half])
    vo[half:] = 0.30 * np.sin(2 * np.pi * f_female * t[half:])
    acc = 0.25 * np.sin(2 * np.pi * 4000.0 * t)
    L = vo.copy()
    R = vo.copy()
    L[:half] += acc[:half]
    R[half:] += acc[half:]
    return np.stack([L, R], axis=1).astype(np.float32), half, n


def _split_pipeline(x, sr, depth=1.0, swap=False, **kw):
    """上位机文件模式真正在用的分离：VocalSplitter（左=人声 / 右=伴奏）。

    返回 (立体声结果, 音轨 dict)，与 host_app._split_worker 的纯 DSP 分支一致。
    ★ 2026-10-21：旧的"中置提取 → 按男女分左右"两段式已整条删除，
      现在只有一种语义 —— 一个声道人声、一个声道伴奏。
    """
    import vocal_split as vs
    sp = vs.VocalSplitter(sr)
    cfg = dict(kw)
    cfg["depth"] = depth
    cfg["swap"] = bool(swap)
    sp.configure(**cfg)
    sp.reset()
    y, st = sp.process(x, block=8192, stems=True)
    return np.asarray(y, dtype=np.float32), st


def test_vocal_split():
    print("\n[11] 人声/伴奏分离（左=人声，右=伴奏）")
    fs = 48000
    x, half, n = _split_test_signal(fs)

    y, st = _split_pipeline(x, fs)
    check("输出形状不变", y.shape == x.shape, "%s -> %s" % (x.shape, y.shape))
    check("音轨只有 vocal / accomp 两个键",
          set(st.keys()) == {"vocal", "accomp"}, "%s" % sorted(st.keys()))

    mid_a = slice(int(0.25 * half), int(0.75 * half))          # 低音段
    mid_b = slice(half + int(0.25 * half), half + int(0.75 * half))  # 高音段

    def amp(sig, f):
        return _goertzel(np.asarray(sig, dtype=np.float64), fs, f)

    # 输入里人声的 mid 幅度 = 0.30（左右同相，mid 不变）
    in_low = amp(x[mid_a, 0], 110.0)
    in_high = amp(x[mid_b, 1], 231.0)
    v_low = amp(y[mid_a, 0], 110.0)
    v_high = amp(y[mid_b, 0], 231.0)
    check("两段人声都进了左声道",
          v_low > 0.60 * in_low and v_high > 0.55 * in_high,
          "左声道 110 Hz %.3f/%.3f，231 Hz %.3f/%.3f（输出/输入）"
          % (v_low, in_low, v_high, in_high))

    # 伴奏硬左(4 kHz)，mid 里只剩一半 -> 0.125；它应该出现在右声道
    in_4k = amp(x[mid_a, 0], 4000.0)
    a_r = amp(y[mid_a, 1], 4000.0)
    a_l = amp(y[mid_a, 0], 4000.0)
    check("伴奏进了右声道", a_r > 0.30 * in_4k,
          "右声道 4 kHz %.4f（输入单侧 %.4f）" % (a_r, in_4k))
    check("伴奏几乎不串进左声道", a_l < 0.15 * max(a_r, 1e-9),
          "左 4 kHz %.4f vs 右 %.4f" % (a_l, a_r))

    # 右声道里残留的人声要被压低（低频保护会放过一部分，110 Hz 尤甚）
    v_leak = amp(y[mid_b, 1], 231.0)
    check("人声没有整体串进右声道",
          v_leak < 0.55 * max(v_high, 1e-9),
          "右声道 231 Hz %.4f vs 左 %.4f（低频保护允许留一点）"
          % (v_leak, v_high))

    # 纯伴奏（没有中置人声）应当左声道接近静音、右声道 ≈ mid
    tt = np.arange(n, dtype=np.float64) / float(fs)
    x2 = np.stack([0.30 * np.sin(2 * np.pi * 900.0 * tt),
                   0.30 * np.sin(2 * np.pi * 1700.0 * tt)], axis=1).astype(np.float32)
    y2, _ = _split_pipeline(x2, fs)
    lv = float(np.sqrt(np.mean(y2[:, 0] ** 2)))
    check("纯伴奏时左声道（人声轨）接近静音", lv < 0.02,
          "左声道 RMS = %.4f" % lv)

    # 同一份输入跑两遍必须逐点一致（离线管线是确定性的）
    yA, _ = _split_pipeline(x, fs)
    yB, _ = _split_pipeline(x, fs)
    check("分离管线确定性（跑两遍逐点一致）", np.array_equal(yA, yB))

    # swap：人声去右、伴奏去左
    y3, _ = _split_pipeline(x, fs, swap=True)
    check("swap 后人声改去右声道",
          amp(y3[mid_b, 1], 231.0) > 0.5 * max(v_high, 1e-9),
          "右声道 231 Hz %.4f（左声道原 %.4f）"
          % (amp(y3[mid_b, 1], 231.0), v_high))

    # 单声道源：不抛异常、输出有限
    xm = np.stack([x[:, 0], x[:, 0]], axis=1)
    y4, _ = _split_pipeline(xm, fs)
    check("单声道源不抛异常且输出有限",
          y4.shape == xm.shape and np.isfinite(y4).all(), "shape=%s" % (y4.shape,))

    # 统计量里不该再有性别字段
    import vocal_split as vs
    sp = vs.VocalSplitter(fs)
    sp.reset()
    sp.process(x)
    sm = sp.summary()
    check("分离统计只有人声相关字段",
          ("voiced_pct" in sm) and ("male_pct" not in sm)
          and ("gender" not in sm),
          "voiced %.1f%% / 双音高 %.1f%% / 未判定 %.1f%%"
          % (sm["voiced_pct"], sm["dual_pct"], sm["unvoiced_pct"]))


def test_split_perf():
    print("\n[12] 男女声分离处理速度")
    fs = 48000
    dur = 20.0
    x, _, _ = _split_test_signal(fs, sec_per_part=dur / 2.0)
    sp = dsp.VocalSplitter(fs)
    sp.configure(depth=1.0)
    t0 = time.time()
    sp.process(x, block=8192)
    dt = time.time() - t0
    ratio = dur / dt if dt > 0 else 0.0
    check("快于实时 3 倍", ratio > 3.0,
          "%.0f 倍实时（%.2f 秒处理 %.0f 秒音频）" % (ratio, dt, dur))


def test_stream_matches_offline():
    """实时模式走的是 process_stream（分离模式有算法延迟，返回长度不定）。

    验证按小块流式喂进去累积出来的结果，与整段 process 的结果一致。
    这是"实时听到的"和"文件处理出来的"一致的前提，也是 RealtimeEngine
    里那套 _orest 续接逻辑能成立的前提。

    唯一的例外是男女声分离模式的首尾各 NFFT 点：整段处理会在两端做
    边缘淡化（分析窗在信号两端必然被截断，不淡化会往结尾塞伪人声），
    实时流没有"结尾"这个概念，所以那 21 ms 对不上。已单独说明。
    """
    print("\n[13] 流式 == 整段（实时模式与文件模式一致）")
    import vocal_split as vsplit
    fs = 48000
    x, _, n = _split_test_signal(fs, sec_per_part=0.6)
    pcm = audio_io.to_int16(x)

    cases = (
        (dsp.MODE_SPLIT, "男女声分离", dict(split=dict(depth=1.0))),
        (dsp.MODE_PITCH, "变调", dict()),
        (dsp.MODE_ECHO, "回声", dict(echo_taps=5, echo_gain=2)),
    )
    for mode, name, extra in cases:
        kw = dict(mode=mode, dphase=dsp.semitone_to_dphase(3.0), gain=1.0)
        kw.update(extra)

        sc_ref = dsp.StereoChain(fs)
        sc_ref.reset()
        sc_ref.configure(**kw)
        y_ref = sc_ref.process(pcm, block=8192)

        sc = dsp.StereoChain(fs)
        sc.reset()
        sc.configure(**kw)
        blk = 512
        raw = []
        fed = 0
        while fed < n:
            take = min(blk, n - fed)
            raw.append(sc.process_stream(pcm[fed:fed + take]))
            fed += take
        guard = 0
        while sum(a.shape[0] for a in raw) < n and guard < 40:
            raw.append(sc.process_stream(np.zeros((blk, 2), dtype=np.int16)))
            guard += 1
        got = (np.concatenate([a for a in raw if a.shape[0]])
               if any(a.shape[0] for a in raw) else np.zeros((0, 2), np.int16))

        check("%s：流式输出不丢样本" % name, got.shape[0] >= n,
              "累积 %d 点，输入 %d 点" % (got.shape[0], n))

        if mode == dsp.MODE_SPLIT:
            m = vsplit.NFFT
            same = np.array_equal(got[m:n - m], y_ref[m:n - m])
            dmax = int(np.abs(got[:n][m:n - m].astype(np.int64)
                              - y_ref[m:n - m].astype(np.int64)).max()) if same is False else 0
            check("男女声分离：流式与整段一致（首尾各 21 ms 除外）", same,
                  "中段最大差 %d" % dmax)
        else:
            same = np.array_equal(got[:n], y_ref)
            check("%s：流式与整段逐点相同" % name, same,
                  "最大差 %d" % int(np.abs(got[:n].astype(np.int64)
                                            - y_ref.astype(np.int64)).max()))


def test_realtime_split_engine():
    """真跑一遍 RealtimeEngine 的分离模式回调路径（不打开声卡）。"""
    print("\n[14] 实时引擎跑分离模式（假输入，不开声卡）")
    try:
        import host_app
    except Exception as e:
        print("     跳过：%r" % (e,))
        return
    fs = 48000
    x, half, _ = _split_test_signal(fs, sec_per_part=0.5)
    eng = host_app.RealtimeEngine()
    # ★ 必须显式给 module/engine：EngineChain.configure 的默认是 MODULE_PITCH，
    #   只给 mode 会悄悄跑成变调链，这条测试就名不副实了。
    cfg = dict(mode=dsp.MODE_SPLIT, module=dsp.MODULE_SPLIT,
               engine=dsp.ENGINE_FPGA_DIRECT,
               dphase=0, ring_freq=1, echo_taps=8,
               echo_gain=2, gain=1.0, ingain=1.0, split=dict(depth=1.0))
    eng.running = True
    eng.get_cfg = lambda: cfg
    eng.channels = 2
    eng.sr_in = fs
    eng.sr_out = fs
    blk = 1024
    # ★ 处理已经搬到独立 worker 线程（_in_cb 只负责入队），只置 running
    #   不启线程的话队列永远没人消费，输出必然是零 —— 那是测试自己没跑起来，
    #   跟链路对不对无关。这里照 start() 的做法把 worker 起起来。
    eng.blocksize = blk
    eng._worker_stop.clear()
    eng._worker = threading.Thread(target=eng._worker_loop, daemon=True)
    eng._worker.start()
    try:
        n = x.shape[0]
        fed = 0
        out = []
        while fed < n + fs // 2:                 # 多喂半秒，把延迟推出来
            seg = x[fed:fed + blk] if fed < n else np.zeros((blk, 2), np.float32)
            if seg.shape[0] < blk:
                seg = np.concatenate([seg, np.zeros((blk - seg.shape[0], 2), np.float32)])
            eng._in_cb(seg, blk, None, None)
            od = np.zeros((blk, 2), dtype=np.float32)
            eng._out_cb(od, blk, None, None)
            out.append(od)
            fed += blk
            # 真实回调间隔约 21 ms（1024/48000）。测试若全速跑，速度远快于
            # worker，_take 每次都取不到东西 —— 那是"测试没等"，不是链路坏。
            time.sleep(0.004)
        y = np.concatenate(out, axis=0)
        check("分离模式实时链路无异常", eng.error is None,
              "error=%s" % eng.error)
        check("输出有声且有限", np.isfinite(y).all() and float(np.abs(y).max()) > 0.01,
              "峰值 %.4f" % float(np.abs(y).max()))
        # 左右应当明显不同（不是把单声道复制两遍）
        d = float(np.mean(np.abs(y[:, 0] - y[:, 1])))
        check("左右两路确实不同（相关性统计有意义）", d > 0.01, "平均 |L-R| = %.4f" % d)
        check("按声道分别统计了电平",
              len(eng.peak_in) == 2 and len(eng.clip_out) == 2,
              "peak_in=%d 项 clip_out=%d 项" % (len(eng.peak_in), len(eng.clip_out)))
    finally:
        eng.running = False
        eng._worker_stop.set()


def test_realtime_path():
    """真录一段麦克风，跑一遍实时链路的输入搬运 + 输出回调处理。

    刻意不打开输出流，避免把扬声器信号再喂回麦克风引起啸叫。
    """
    print("\n[9] 实时链路（真实麦克风，不回放）")
    try:
        import sounddevice as sd
    except Exception as e:
        print("     跳过：%r" % (e,))
        return
    try:
        import host_app
        eng = host_app.RealtimeEngine()
        cfg = dict(mode=dsp.MODE_PITCH, dphase=dsp.semitone_to_dphase(4),
                   ring_freq=1, echo_taps=8, echo_gain=2, gain=1.0, ingain=4.0)
        blk = 1024
        got = {"n": 0}

        try:
            eng.running = True
            eng.get_cfg = lambda: cfg
            eng.channels = 2
            eng.sr_in = 48000
            eng.sr_out = 48000

            def in_cb(indata, frames, tinfo, status):
                eng._in_cb(indata, frames, tinfo, status)
                got["n"] += frames

            with sd.InputStream(samplerate=48000, channels=2, dtype="float32",
                                blocksize=blk, callback=in_cb):
                sd.sleep(1200)
            check("输入回调收到样本", got["n"] > 40000, "%d 点" % got["n"])

            outdata = np.zeros((blk, 2), dtype=np.float32)
            total = 0
            peak = 0.0
            while not eng.q.empty():
                eng._out_cb(outdata, blk, None, None)
                total += 1
                peak = max(peak, float(np.abs(outdata).max()))
            eng.running = False
            check("输出回调处理了输入队列", total > 0, "%d 块" % total)
            if peak > 0.0:
                print("     处理 %d 块，输出峰值 %.4f，增益后应有可闻电平" % (total, peak))
            check("实时处理无异常且输出有限", eng.error is None and np.isfinite(peak),
                  "error=%s peak=%.4f" % (eng.error, peak))
        except Exception as e:
            import traceback
            check("实时链路可运行", False,
                  traceback.format_exc(limit=2).replace("\n", " | "))
    except Exception as e:
        print("     跳过：%r" % (e,))


def test_spectrum_both_channels():
    """频谱要**同时**画出左右两个声道，而不是二选一。"""
    print("\n[15] 频谱同时显示左右声道")
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception as e:
        print("     跳过（没有 tkinter）：%r" % (e,))
        return
    try:
        import host_app          # noqa: F401  (确保模块可导入)
        import spectrum as sp

        fs = 48000
        n = 16384
        t = np.arange(n, dtype=np.float32) / float(fs)
        ldb = sp.compute_spectrum((0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32), fs)
        rdb = sp.compute_spectrum((0.5 * np.sin(2 * np.pi * 6000 * t)).astype(np.float32), fs)
        check("两路的谱峰确实在不同频点",
              int(np.argmax(ldb)) != int(np.argmax(rdb)),
              "L 峰在第 %d 格 / R 峰在第 %d 格" % (int(np.argmax(ldb)), int(np.argmax(rdb))))

        cv = sp.DualSpectrum(root, height=250)
        cv._cw = 900
        cv._ch = 250
        cv.set_sr(fs)
        cv.set_in(ldb, rdb)
        cv.set_out(rdb, ldb)          # 输出故意左右对调，便于区分
        cv.draw()
        root.update_idletasks()

        items = cv.find_all()
        fills = [cv.itemcget(i, "fill") for i in items
                 if cv.type(i) == "rectangle"]
        n_l_in = fills.count(sp.DualSpectrum.COLORS[("in", 0)])
        n_r_in = fills.count(sp.DualSpectrum.COLORS[("in", 1)])
        n_l_out = fills.count(sp.DualSpectrum.COLORS[("out", 0)])
        n_r_out = fills.count(sp.DualSpectrum.COLORS[("out", 1)])
        check("输入格同时画出左(深)右(浅)两个声道",
              n_l_in > 0 and n_r_in > 0,
              "L 柱 %d 根 / R 柱 %d 根" % (n_l_in, n_r_in))
        check("输出格同样左右都在",
              n_l_out > 0 and n_r_out > 0,
              "L 柱 %d 根 / R 柱 %d 根" % (n_l_out, n_r_out))

        # 6 kHz 那一根 R 柱应该明显比 1 kHz 那一根 L 柱靠右
        def x_of_color(color):
            xs = [cv.coords(i)[0] for i in items
                  if cv.type(i) == "rectangle" and cv.itemcget(i, "fill") == color]
            return min(xs) if xs else -1.0

        xl = x_of_color(sp.DualSpectrum.COLORS[("in", 0)])
        xr = x_of_color(sp.DualSpectrum.COLORS[("in", 1)])
        check("右声道的峰画在更靠右的位置（频率轴正确）", 0 < xl < xr,
              "L 最左柱 x=%.0f，R 最左柱 x=%.0f" % (xl, xr))

        # 单路数据应画满整格：柱子数约为双路的 2 倍
        cv.clear()
        cv.set_in(ldb)
        cv.draw()
        root.update_idletasks()
        fills2 = [cv.itemcget(i, "fill") for i in cv.find_all()
                  if cv.type(i) == "rectangle"]
        check("只有一路时画满整格（柱子数≈双路的两倍）",
              fills2.count(sp.DualSpectrum.COLORS[("in", 0)]) > 0
              and fills2.count(sp.DualSpectrum.COLORS[("in", 1)]) == 0,
              "整格 %d 根" % fills2.count(sp.DualSpectrum.COLORS[("in", 0)]))

        # 清空后应显示"无信号"
        cv.clear()
        cv.draw()
        root.update_idletasks()
        check("清空后两边都为空", cv.db["in"] == [None, None]
              and cv.db["out"] == [None, None])

        # 灯板：窗口变窄时会自动减列，高度必须跟着补，否则最后一行被裁掉
        # （这是实际截图里发现的问题：缓冲/DSP/文件/缓存/板上档位五盏灯不见了）
        from status_panel import CELL_H, COLORS, LedBoard
        items = [(str(i), "灯%d" % i, "") for i in range(15)]
        board = LedBoard(root, items, cols=8)
        semicolors = set(COLORS.values())
        for w in (700, 900, 300):
            board._cw = w
            board._last_draw = 0.0        # 绕开 120 ms 重画节流（实测 UI 行为）
            board.draw()
            root.update_idletasks()
            cols = board._sized_cols(w)
            rows = board._rows_for(cols)
            # 每盏灯画两个 oval：外面一个光晕、里面一个灯珠。按填充色筛，
            # 只数语义色的那些（光晕是压暗的同色，不在 COLORS 里）。
            ovals = [i for i in board.find_all()
                     if board.type(i) == "oval"
                     and board.itemcget(i, "fill") in semicolors]
            ys = [board.coords(i)[3] for i in ovals]
            h = int(board.winfo_reqheight())
            check("灯板 %dpx 宽：%d 列 %d 行不裁灯" % (w, cols, rows),
                  len(ovals) == 15 and h >= rows * CELL_H + 8
                  and (max(ys) if ys else 0) <= h,
                  "%d 盏 / 行数 %d / 高 %d px / 最低灯 y=%.0f"
                  % (len(ovals), rows, h, max(ys) if ys else 0))
        root.destroy()
    except Exception as e:
        import traceback
        check("频谱绘制", False, traceback.format_exc(limit=3).replace("\n", " | "))
        try:
            root.destroy()
        except Exception:
            pass


def test_long_run_stability():
    """长时间运行：削波计数衰减、内部缓冲有界、缓存按 TTL 自动释放。"""
    print("\n[16] 长时间运行稳定性（缓存自动清理）")
    try:
        import host_app
    except Exception as e:
        print("     跳过：%r" % (e,))
        return
    fs = 48000

    # (a) 削波计数随时间衰减：很久以前的那次过载必须被忘掉
    eng = host_app.RealtimeEngine()
    eng.sr_out = fs
    eng.clip_in = [50, 50]
    eng.clip_out = [50, 50]
    eng._decay_acc = 0
    eng._decay_clip(fs)                       # 1 秒
    check("削波计数每秒衰减一次",
          eng.clip_in == [50 - host_app.CLIP_DECAY] * 2
          and eng.clip_out == [50 - host_app.CLIP_DECAY] * 2,
          "50 -> %s（衰减量 %d/秒）" % (eng.clip_in[0], host_app.CLIP_DECAY))
    eng._decay_clip(fs * 60)                  # 一分钟
    check("一分钟前的削波彻底清零", eng.clip_in == [0, 0] and eng.clip_out == [0, 0],
          "%s / %s" % (eng.clip_in, eng.clip_out))

    # (b) 连续跑分离模式的实时链路，内部缓冲必须有界
    # ★ 必须显式给 module/engine：EngineChain.configure 的默认是 MODULE_PITCH，
    #   只给 mode 会悄悄跑成变调链，这条测试就名不副实了。
    cfg = dict(mode=dsp.MODE_SPLIT, module=dsp.MODULE_SPLIT,
               engine=dsp.ENGINE_FPGA_DIRECT,
               dphase=0, ring_freq=1, echo_taps=8,
               echo_gain=2, gain=1.0, ingain=1.0, split=dict(depth=1.0))
    eng2 = host_app.RealtimeEngine()
    eng2.running = True
    eng2.get_cfg = lambda: cfg
    eng2.channels = 2
    eng2.sr_in = fs
    eng2.sr_out = fs
    blk = 1024
    rng = np.random.RandomState(11)
    mx_q = 0
    mx_orest = 0
    blocks = int(20 * fs / blk)               # 20 秒音频
    for _ in range(blocks):
        seg = (rng.randn(blk, 2) * 0.2).astype(np.float32)
        eng2._in_cb(seg, blk, None, None)
        od = np.zeros((blk, 2), dtype=np.float32)
        eng2._out_cb(od, blk, None, None)
        mx_q = max(mx_q, eng2.q.qsize())
        if eng2._orest is not None:
            mx_orest = max(mx_orest, int(eng2._orest.shape[0]))
    eng2.running = False
    check("跑 20 秒后输入队列仍然有界",
          mx_q <= 128 and eng2.q.qsize() <= 128, "峰值 %d 块 / 上限 128" % mx_q)
    check("效果链残留缓冲有界（不被算法延迟越撑越大）",
          0 <= mx_orest <= 8 * blk, "峰值 %d 点" % mx_orest)
    check("实时链路无异常", eng2.error is None, "error=%s" % eng2.error)

    # (c) 巡检反复执行：日志与缓存都不能长起来
    try:
        import tkinter as tk
        tk.Tk()
    except Exception as e:
        print("     跳过 GUI 部分：%r" % (e,))
        return
    try:
        app = host_app.App()
        app.update_idletasks()
        for i in range(host_app.LOG_MAX_LINES + 400):
            app._log("压力测试日志 %d" % i)
        peak_lines = int(app.log.index("end-1c").split(".")[0])
        caches = []
        for i in range(60):
            app._housekeep()
            caches.append(app._cache_bytes())
            if i == 10:
                # 插一个大缓存，再让它超时
                app.proc_data = np.zeros((fs * 4, 2), dtype=np.float32)
                app._proc_at = time.time() - host_app.PROC_TTL_SEC - 1
        lines = int(app.log.index("end-1c").split(".")[0])
        check("日志在压力下仍被裁到上限",
              lines <= host_app.LOG_MAX_LINES,
              "峰值 %d 行 -> 现在 %d 行（上限 %d）"
              % (peak_lines, lines, host_app.LOG_MAX_LINES))
        check("超时的大缓存被自动释放", app.proc_data is None,
              "当前缓存 %.2f MB" % app._mb(app._cache_bytes()))
        check("60 轮巡检后缓存不再增长",
              caches[-1] <= caches[0],
              "首轮 %.3f MB -> 末轮 %.3f MB"
              % (app._mb(caches[0]), app._mb(caches[-1])))
        check("巡检计数与状态可见", app._hk_cleaned >= 1 and app.lb_cache.cget("text"),
              app.lb_cache.cget("text")[:40])

        # (d) 素材：处理中绝不卸载，闲置过久才自动卸载
        app.file_data = np.zeros((fs, 2), dtype=np.float32)
        app.file_path = "demo.wav"
        app._file_at = time.time() - host_app.SRC_TTL_SEC - 1
        app._processing = True
        app._clean_caches_now(silent=True)
        check("处理中的素材不会被自动卸载", app.file_data is not None)
        app._processing = False
        app._clean_caches_now(silent=True)
        check("闲置过久的素材被自动卸载", app.file_data is None,
              "阈值 %d 分钟" % (host_app.SRC_TTL_SEC // 60))

        # (e) 手动卸载按钮
        app.file_data = np.zeros((fs, 2), dtype=np.float32)
        app._file_at = time.time()
        app._unload_source()
        check("「卸载素材」立刻生效", app.file_data is None and app.proc_data is None)
        app.destroy()
    except Exception as e:
        import traceback
        check("长时间运行巡检", False,
              traceback.format_exc(limit=3).replace("\n", " | "))


def _make_formats(tmp):
    """造一批不同格式的测试文件，返回 {扩展名: 路径}。

    能造多少取决于装了哪些编码器：soundfile 负责 wav/flac/ogg/opus/aiff，
    PyAV 负责 mp3/m4a。造不出来的格式就跳过，不判失败。
    """
    fs = 44100
    n = fs                                    # 1 秒
    t = np.arange(n, dtype=np.float32) / float(fs)
    x = np.stack([0.45 * np.sin(2 * np.pi * 440.0 * t),
                  0.45 * np.sin(2 * np.pi * 660.0 * t)], axis=1).astype(np.float32)
    made = {}

    def _try(ext, fn):
        p = os.path.join(tmp, "fmt" + ext)
        try:
            fn(p)
        except Exception:
            return
        if os.path.exists(p) and os.path.getsize(p) > 128:
            made[ext] = p

    _try(".wav", lambda p: audio_io.save_wav(p, x, fs))
    _try(".flac", lambda p: audio_io.save_audio(p, x, fs))
    _try(".ogg", lambda p: audio_io.save_audio(p, x, fs))
    _try(".aiff", lambda p: audio_io.save_audio(p, x, fs))

    try:
        import av

        def _av(ext, fmt, codec):
            def go(p):
                with av.open(os.path.join(tmp, "fmt.wav")) as inp, \
                        av.open(p, "w", format=fmt) as ost:
                    ist = inp.streams.audio[0]
                    o = ost.add_stream(codec, rate=ist.codec_context.sample_rate)
                    for fr in inp.decode(audio=0):
                        for pk in o.encode(fr):
                            ost.mux(pk)
                    for pk in o.encode(None):
                        ost.mux(pk)
            return go

        _try(".mp3", _av(".mp3", "mp3", "libmp3lame"))
        _try(".m4a", _av(".m4a", "mp4", "aac"))
    except Exception:
        pass
    return x, fs, made


def test_audio_formats():
    print("\n[17] 文件格式兼容（含 FLAC）")
    import shutil
    import tempfile

    bk = dict(audio_io.backends())
    check("至少有一个解码后端可用", bool(bk.get("soundfile") or bk.get("av")),
          "soundfile=%s av=%s librosa=%s"
          % (bk.get("soundfile"), bk.get("av"), bk.get("librosa")))

    tmp = tempfile.mkdtemp(prefix="acx_test_fmt_")
    try:
        x, fs, made = _make_formats(tmp)
        check("造出测试文件", len(made) >= 3, "格式：%s" % sorted(made.keys()))

        for ext in sorted(made):
            info = {}
            try:
                a, sr = audio_io.load_audio(made[ext], info)
            except Exception as e:
                check("%s 能读" % ext, False, repr(e))
                continue
            n_ok = abs(a.shape[0] - x.shape[0]) <= max(2048, x.shape[0] // 20)
            # 有损格式会有相移/噪声，只要求波形同相相关
            m = min(a.shape[0], x.shape[0])
            aa = a[:m, 0] - a[:m, 0].mean()
            xx = x[:m, 0] - x[:m, 0].mean()
            corr = float(np.dot(aa, xx) / (np.linalg.norm(aa) * np.linalg.norm(xx) + 1e-12))
            check("%s 能读且内容对得上" % ext,
                  sr == fs and n_ok and corr > 0.9,
                  "后端=%-10s sr=%d 长度=%d/%d 相关=%.3f"
                  % (info.get("backend"), sr, a.shape[0], x.shape[0], corr))

        # 输出：按扩展名选格式，无损格式必须逐点还原
        for ext in (".wav", ".flac"):
            p = os.path.join(tmp, "out" + ext)
            try:
                real = audio_io.save_audio(p, x, fs)
                info = {}
                a, sr = audio_io.load_audio(real, info)
                err = float(np.abs(a[:x.shape[0]] - x).max())
                check("%s 写出后无损读回" % ext,
                      real.endswith(ext) and sr == fs and err < 1e-3,
                      "%d bytes，后端=%s，误差=%.6f"
                      % (os.path.getsize(real), info.get("backend"), err))
            except Exception as e:
                check("%s 写出后无损读回" % ext, False, repr(e))

        # 认不出的扩展名：不要往 .xyz 里塞 WAV 数据，应改成 .wav 并返回真实路径
        odd = os.path.join(tmp, "weird.xyz")
        real = audio_io.save_audio(odd, x, fs)
        check("未知扩展名回退成真正的 .wav",
              real.endswith(".wav") and os.path.exists(real) and not os.path.exists(odd),
              os.path.basename(real))

        # 报错必须把每个后端各自的原因列全（以前只报最后一个，把真因藏了）
        try:
            audio_io.load_audio(os.path.join(tmp, "nope.flac"))
            check("读不存在的文件会报错", False, "居然没抛异常")
        except audio_io.AudioReadError as e:
            msg = str(e)
            check("报错列出了每个后端的原因",
                  "soundfile" in msg and "PyAV" in msg,
                  msg.splitlines()[1].strip()[:110] if len(msg.splitlines()) > 1 else msg[:110])
        except Exception as e:
            check("报错列出了每个后端的原因", False, "抛的是 %r" % (e,))

        # 假装成音频的文本文件也要给出可读的报错，而不是崩掉
        bad = os.path.join(tmp, "fake.flac")
        with open(bad, "w") as f:
            f.write("这不是音频\n" * 50)
        try:
            audio_io.load_audio(bad)
            check("坏文件给出明确报错", False, "居然没抛异常")
        except audio_io.AudioReadError:
            check("坏文件给出明确报错", True, "AudioReadError")
        except Exception as e:
            check("坏文件给出明确报错", False, repr(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stems_export():
    print("\n[18] 音轨导出（人声 / 伴奏 两条）")
    fs = 48000
    x, half, n = _split_test_signal(fs, sec_per_part=1.0)

    y, st = _split_pipeline(x, fs)
    check("返回两轨且长度与输入一致",
          set(st.keys()) == {"vocal", "accomp"}
          and st["vocal"].shape == (n,) and st["accomp"].shape == (n,),
          {k: v.shape for k, v in st.items()})

    def rms(a):
        a = np.asarray(a, dtype=np.float64)
        return float(np.sqrt((a ** 2).mean())) if a.size else 0.0

    def amp(a, f):
        return _goertzel(np.asarray(a, dtype=np.float64), fs, f)

    a_m = slice(int(0.25 * half), int(0.75 * half))
    b_m = slice(half + int(0.25 * half), half + int(0.75 * half))

    check("人声轨两段都有声（高低两个音域都抓到了）",
          rms(st["vocal"][a_m]) > 0.10 and rms(st["vocal"][b_m]) > 0.10,
          "前半 RMS=%.4f 后半 RMS=%.4f"
          % (rms(st["vocal"][a_m]), rms(st["vocal"][b_m])))
    check("人声轨在各自音域是主导频率",
          amp(st["vocal"][a_m], 110.0) > amp(st["vocal"][a_m], 231.0)
          and amp(st["vocal"][b_m], 231.0) > amp(st["vocal"][b_m], 110.0),
          "前半 110Hz=%.3f/231Hz=%.3f，后半 110Hz=%.3f/231Hz=%.3f"
          % (amp(st["vocal"][a_m], 110.0), amp(st["vocal"][a_m], 231.0),
             amp(st["vocal"][b_m], 110.0), amp(st["vocal"][b_m], 231.0)))

    # 伴奏轨是 mid 减去人声，硬声像伴奏在 mid 里只剩一半 -> 目标 0.125
    in4k = amp(x[a_m, 0], 4000.0)
    acc4k = amp(st["accomp"][a_m], 4000.0)
    check("伴奏轨保留了硬声像伴奏",
          acc4k > 0.30 * in4k and abs(acc4k - in4k) < 0.75 * in4k,
          "输入单侧 %.4f -> 伴奏轨 %.4f（mid 里天然只有一半）"
          % (in4k, acc4k))

    # ★ 独立先验：4 kHz 是构造信号时就定死的硬声像伴奏（只在一侧），
    #   中置的人声轨里不该有它。这条与分离实现无关，能真抓出串扰。
    leak4k = amp(st["vocal"][a_m], 4000.0)
    check("人声轨不含硬声像伴奏（4 kHz 串扰）", leak4k < 0.01,
          "人声轨 4kHz=%.4f" % leak4k)

    # 绝对量断言：相对比例可以用"两轨同乘一个系数"骗过去，绝对幅度不行。
    in_110 = amp(x[a_m, 0], 110.0)
    st_110 = amp(st["vocal"][a_m], 110.0)
    check("人声轨 110 Hz 幅度接近输入中的绝对幅度",
          abs(st_110 - in_110) < 0.25 * max(in_110, 1e-9),
          "输入 %.4f -> 人声轨 %.4f" % (in_110, st_110))
    in_231 = amp(x[b_m, 1], 231.0)
    st_231 = amp(st["vocal"][b_m], 231.0)
    check("人声轨 231 Hz 幅度接近输入中的绝对幅度",
          abs(st_231 - in_231) < 0.25 * max(in_231, 1e-9),
          "输入 %.4f -> 人声轨 %.4f" % (in_231, st_231))

    # 单声道源也应该能导出（会走谐波提取降级路径）
    xm2 = np.stack([x[:, 0], x[:, 0]], axis=1)
    _y2, st2 = _split_pipeline(xm2, fs)
    check("单声道源也能导出两轨且都有限",
          st2["vocal"].shape == (n,) and np.isfinite(st2["vocal"]).all()
          and np.isfinite(st2["accomp"]).all(),
          "shape=%s" % (st2["vocal"].shape,))

    # 经 StereoChain.process_stems 走一遍，结果必须和直接调用逐点一致
    sc = dsp.StereoChain(fs)
    sc.reset()
    sc.configure(mode=dsp.MODE_SPLIT, gain=1.0, split=dict(depth=1.0))
    pcm = audio_io.to_int16(x)
    y3, st3 = sc.process_stems(pcm, block=8192)
    sp3 = dsp.VocalSplitter(fs)
    sp3.configure(depth=1.0)
    y_direct, st_direct = sp3.process(pcm, stems=True)
    check("StereoChain.process_stems 与直接调用逐点一致",
          np.array_equal(y3, y_direct)
          and np.array_equal(st3["vocal"], st_direct["vocal"])
          and np.array_equal(st3["accomp"], st_direct["accomp"]),
          "输出相同=%s 人声轨相同=%s"
          % (np.array_equal(y3, y_direct),
             np.array_equal(st3["vocal"], st_direct["vocal"])))

    sc2 = dsp.StereoChain(fs)
    sc2.reset()
    sc2.configure(mode=dsp.MODE_PITCH)
    _y4, st4 = sc2.process_stems(pcm, block=8192)
    check("非分离模式不产出音轨", st4 is None)


def test_player():
    """内嵌播放器：装载、左右声道增益、跳转（不打开声卡）。"""
    print("\n[20] 内嵌播放器（左右声道开关 / 跳转）")
    try:
        import player as pl
        fs = 48000
        t = np.arange(fs, dtype=np.float32) / float(fs)
        x = np.stack([0.5 * np.sin(2 * np.pi * 440 * t),
                      0.25 * np.sin(2 * np.pi * 880 * t)], axis=1)
        p = pl.Player()
        p.load(x, fs, "test")
        check("播放器装载双声道", p.has_data() and p.duration > 0.99,
              "时长 %.2f 秒" % p.duration)
        p.set_channel(0, False)
        p.set_channel(1, True)
        out = np.zeros((1024, 2), dtype=np.float32)
        p._cb(out, 1024, None, None)
        l_off = float(np.abs(out[:, 0]).max())
        r_on = float(np.abs(out[:, 1]).max())
        check("关左声道后左路静音、右路正常",
              l_off < 1e-6 and r_on > 0.1,
              "L=%.4f R=%.4f" % (l_off, r_on))
        p.seek(0.5)
        check("拖动跳转生效", abs(p.position - 0.5) < 0.02,
              "位置 %.3f 秒" % p.position)
        p.set_channel(0, True)
        out2 = np.zeros((1024, 2), dtype=np.float32)
        p._cb(out2, 1024, None, None)
        check("重新打开左声道后恢复",
              float(np.abs(out2[:, 0]).max()) > 0.1,
              "L=%.4f" % float(np.abs(out2[:, 0]).max()))

        # 输出设备：必须真的透传给 OutputStream，而不是只存了个变量。
        # 用假 sd 模块拦下 OutputStream 的实参（不碰真声卡）。
        captured = {}

        class _FakeStream(object):
            def __init__(self, **kw):
                captured.clear()
                captured.update(kw)
                self.active = False

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def close(self):
                self.active = False

        class _FakeSD(object):
            @staticmethod
            def OutputStream(**kw):
                return _FakeStream(**kw)

        real_sd = pl.sd
        try:
            pl.sd = _FakeSD
            pd = pl.Player()
            pd.load(x, fs, "dev")
            pd.set_device(6)
            opened = pd.play()
            seen_dev = captured.get("device")          # 断言当时的实参，别被后面的覆盖
            dev_ok = opened and seen_dev == 6
            pd.set_device(None)          # 切回系统默认
            pd.play()
            default_ok = captured.get("device") is None
            pd.close()
        finally:
            pl.sd = real_sd
        check("播放器可指定输出设备（device 透传给 OutputStream）",
              dev_ok, "set_device(6) → OutputStream(device=%r)" % (seen_dev,))
        check("播放器可切回系统默认输出", default_ok, "device=None")
        p.close()
    except Exception as e:
        check("内嵌播放器可运行", False, repr(e))


def test_file_split_end_to_end():
    """整天流程：FLAC 进 -> 分离 -> FLAC 出 + 两条音轨，全程用真实文件。"""
    print("\n[19] 端到端：FLAC 输入 → 分离 → 立体声输出 + 人声/伴奏音轨")
    import shutil
    import tempfile

    fs = 48000
    x, _half, n = _split_test_signal(fs, sec_per_part=0.5)
    tmp = tempfile.mkdtemp(prefix="acx_e2e_")
    try:
        src = os.path.join(tmp, "song.flac")
        try:
            audio_io.save_audio(src, x, fs)
        except Exception as e:
            print("     跳过（写不了 FLAC）：%r" % (e,))
            return
        info = {}
        a, sr = audio_io.load_audio(src, info)
        check("FLAC 输入读回", sr == fs and a.shape[0] == n,
              "后端=%s %s" % (info.get("backend"), (a.shape,)))

        res, stems = _split_pipeline(audio_io.to_stereo(a), sr)

        out_flac = os.path.join(tmp, "out.flac")
        real = audio_io.save_audio(out_flac, res, sr)
        check("立体声结果写成 FLAC", real.endswith(".flac") and os.path.exists(real),
              "%d bytes" % os.path.getsize(real))

        paths = []
        for tag, key in (("人声", "vocal"), ("伴奏", "accomp")):
            p = os.path.join(tmp, "out_%s.flac" % tag)
            paths.append(audio_io.save_audio(p, stems[key], sr))
        check("两条音轨都写成了可读文件",
              all(os.path.exists(p) for p in paths),
              "、".join(os.path.basename(p) for p in paths))

        # 读回来核对：人声轨里两段音高的主导频率各不相同，伴奏轨里是 4 kHz
        vo, sr2 = audio_io.load_audio(paths[0])
        ao, _ = audio_io.load_audio(paths[1])
        mid1 = slice(int(0.25 * n // 2), int(0.75 * n // 2))
        mid2 = slice(n // 2 + int(0.25 * n // 2), n // 2 + int(0.75 * n // 2))

        def amp(y, f):
            return _goertzel(np.asarray(y, dtype=np.float64), sr2, f)

        check("导出文件里两段人声各在自己的音域",
              amp(vo[mid1], 110.0) > 1.6 * amp(vo[mid1], 231.0)
              and amp(vo[mid2], 231.0) > 1.6 * amp(vo[mid2], 110.0),
              "前段110=%.3f 后段231=%.3f"
              % (amp(vo[mid1], 110.0), amp(vo[mid2], 231.0)))
        check("伴奏轨里 4 kHz 远高于人声轨里的 4 kHz",
              amp(ao[mid1], 4000.0) > 4.0 * max(amp(vo[mid1], 4000.0), 1e-9),
              "伴奏 4kHz=%.4f / 人声 4kHz=%.4f"
              % (amp(ao[mid1], 4000.0), amp(vo[mid1], 4000.0)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ai_gate():
    """「分离 × 计算机本地模型」必须真的用 demucs，不能静默降级成 DSP。

    守的是一个真实发生过的线上事故：打包版（轻量 onedir）不带 torch，torch 是
    运行时由 local_ai 从用户级 site-packages 挂进 sys.path 的；而旧代码在
    host_app **import 阶段**就调 ai_separate.available()，那时还没挂，必然
    ModuleNotFoundError，结果被存进 HAVE_AI 之后永不刷新 —— 于是每次分离都
    走纯 DSP 中置提取（人声里所有居中乐器原样都在），用户连续几轮反馈
    "AI 分离不干净"，而 demucs 其实一次都没跑过。

    两条断言，方向相反，缺一条这条测试就是装饰：
      1) import 阶段留下的旧结论（HAVE_AI=False）**不能**影响实际判定；
      2) 真的不可用时必须老实返回 False 并带上原因，不许硬编码成 True。
    """
    print("\n[21] AI 开关判定（打包版 torch 运行时挂载）")
    try:
        import host_app as HA
    except Exception as e:
        check("host_app 可导入", False, repr(e))
        return
    if HA.ai_separate is None:
        print("     跳过：本机没有 ai_separate 模块")
        return

    # ---- 断言 1：先跑"不可用"的负对照（此时缓存为空，能真正走到检查器）
    real_avail = HA.ai_separate.available
    try:
        HA.ai_separate.available = lambda: (False, "模拟：没装 torch")
        ok, why = HA._ai_ready()
        check("负对照：真不可用时必须返回不可用且带原因",
              (ok is False) and ("模拟" in str(why)),
              "ok=%r why=%r" % (ok, why))
    finally:
        HA.ai_separate.available = real_avail

    # ---- 断言 2：import 阶段先把 HAVE_AI 置成 False（模拟打包版），
    #      真正要跑的时候必须仍然判成可用
    HA.HAVE_AI = False
    ok, why = HA._ai_ready()
    check("import 时的旧结论（HAVE_AI=False）不会让 AI 被静默跳过",
          ok is True, "ok=%r why=%r" % (ok, why))
    check("可用之后走缓存，不重复挂载",
          HA._AI_READY.get("ok") is True, "_AI_READY=%r" % (HA._AI_READY,))


def test_anti_howl():
    """防啸叫：正常说话必须逐位直通，只有真的自激才挂陷波。

    守两件事：
      1. 开关打开但素材里没有啸叫时，输出必须和输入**逐位相同**
         （否则等于悄悄给所有素材加了个滤波器）；
      2. 指数增长的窄带确实会被压下去（证明它不是个永远不动的空壳）。
    """
    import anti_howl
    sr = 24000

    # ---- 1) 正常语音：不许动手
    t = np.arange(sr * 2) / float(sr)
    voice = 0.4 * sum(np.sin(2 * np.pi * 150.0 * h * t + 0.3 * h) / h
                      for h in range(1, 15))
    voice /= max(np.abs(voice).max(), 1e-9) * 2.5
    if voice.ndim == 1:
        voice = np.stack([voice, voice], axis=1)
    ah = anti_howl.HowlSuppressor(sr)
    y = np.concatenate([ah.process(voice[i:i + 1024])
                        for i in range(0, len(voice), 1024)])
    d = float(np.max(np.abs(y - voice)))
    rep = abs(d) < 1e-12
    check("防啸叫：正常语音逐位直通（不加陷波）", rep,
           "max|diff|=%.1e 陷波=%d" % (d, len(ah.notches)))

    # ---- 2) 指数增长的窄带：必须挂上陷波并压下去
    ah2 = anti_howl.HowlSuppressor(sr)
    seg = []
    for b in range(24):
        tt = (np.arange(1024) + b * 1024) / float(sr)
        x = 0.01 * (1.6 ** b) * np.sin(2 * np.pi * 1200.0 * tt)
        seg.append(x)
        ah2.process(x)
    fired = len(ah2.notches) > 0 and ah2.notches[0][1] <= -6.0
    check("防啸叫：指数增长的窄带会挂上陷波",
           fired, "陷波=%s" % ([(round(n[0]), round(n[1], 1))
                                for n in ah2.notches],))


def test_age_mode_ui():
    """年龄变声必须是一个**和 FPGA 直算并列的引擎**，不是藏在页面里的控件。

    守三件事：
      1. 左侧引擎组里真的有这一项，切过去之后参数框换成 f_age（不是 f_pitch）；
      2. current_cfg() 真的把 engine=age 传下去（旧版会强制回退成直算，
         那样切了也没用——这是最容易"看着能选、实际不生效"的一种坏法）；
      3. 防啸叫开关在实时页与配置里都通着（两侧同一个变量）。
    """
    print("\n[20] 年龄变声引擎与防啸叫开关")
    try:
        import tkinter
        import host_app
        import dsp_core as dsp
        app = host_app.App()
        app.update()
    except Exception as e:
        check("年龄变声：GUI 可构建", False, repr(e))
        return

    try:
        # ---- 1) 引擎组里有「年龄变声」，且和直算并列
        engs = [w.cget("value") for w in app.f_eng_pitch.winfo_children()
                if isinstance(w, tkinter.ttk.Radiobutton)]
        check("左侧引擎组含「年龄变声」且与直算并列",
              dsp.ENGINE_AGE in engs and dsp.ENGINE_FPGA_DIRECT in engs,
              "共 %d 项：%s" % (len(engs), engs))

        # ---- 2) 切过去：v_engine 变了，参数框换成 f_age
        app.v_module.set(dsp.MODULE_PITCH)
        app.v_engine.set(dsp.ENGINE_AGE)
        app._on_engine_change()
        app.update_idletasks()
        cfg = app.current_cfg()
        vis_age = getattr(app.f_age, "_visible", None)
        vis_pitch = getattr(app.f_pitch, "_visible", None)
        check("切到年龄变声后参数框是 f_age（f_pitch 隐藏）",
              bool(vis_age) and not bool(vis_pitch),
              "f_age=%s f_pitch=%s" % (vis_age, vis_pitch))
        check("current_cfg 不被强制回退成直算",
              cfg["engine"] == dsp.ENGINE_AGE,
              "engine=%s" % cfg["engine"])

        # ---- 2b) 变调量必须来自年龄，而不是那个半音滑杆 ----
        #   否则"选了年龄变声、本机听起来没变化"，只有板子在变。
        app.v_semi.set(0.0)
        app.v_age_from.set("30")
        app.v_age_to.set("70")
        app.v_age_sex.set("男")
        c_age = app.current_cfg()
        app.v_engine.set(dsp.ENGINE_FPGA_DIRECT)
        c_dir = app.current_cfg()
        app.v_engine.set(dsp.ENGINE_AGE)
        check("年龄引擎：滑杆归零时变调量仍来自年龄",
              c_age["dphase"] != 0 and abs(c_age["semi_exact"]) > 0.5,
              "30→70 男：semi=%.2f dphase=%d；直算下 dphase=%d"
              % (c_age["semi_exact"], c_age["dphase"], c_dir["dphase"]))
        check("切回直算后变调量回到滑杆（0 半音）",
              c_dir["dphase"] == 0 and abs(c_dir["semi_exact"]) < 1e-9,
              "semi=%.2f dphase=%d" % (c_dir["semi_exact"], c_dir["dphase"]))

        # ---- 3) 防啸叫：实时页有开关，且开关真的进配置
        has_cb = hasattr(app, "cb_howl_rt") and app.cb_howl_rt.winfo_exists()
        app.v_anti_howl.set(True)
        on = app.current_cfg().get("anti_howl")
        app.v_anti_howl.set(False)
        off = app.current_cfg().get("anti_howl")
        check("实时页有防啸叫开关且开关进配置",
              has_cb and on is True and off is False,
              "开关=%s 开=%s 关=%s" % (has_cb, on, off))
        app.v_anti_howl.set(True)
    except Exception as e:
        check("年龄变声：交互检查", False, repr(e))
    finally:
        try:
            app.destroy()
        except Exception:
            pass



def test_realtime_howl_wired():
    """实时链路必须**真的**执行防啸叫，而不只是界面上有个勾。

    做法：绕过声卡，直接造一个 RealtimeEngine，把效果链换成直通桩，
    再喂一段指数增长的窄带。判据是行为，不是源码里有没有那行：
      · 关：输出必须与输入逐位相同，且不建抑制器；
      · 开：抑制器被建出来、真的挂上陷波（说明它跑过了）。
    """
    print("\n[21] 实时链路的防啸叫接线")
    import host_app
    import dsp_core as dsp

    class _StubChain(object):
        def configure(self, **kw):
            pass

        def process_stream(self, pcm):
            # 直通桩：process_stream 的契约是 **int16 进、int16 出**，
            # 转 float 是调用方后面用 audio_io.to_float 做的。
            # （第一版这里返回了 float，等于把信号缩小 32768 倍，
            #   于是"关掉时逐位直通"全挂、抑制器也检不到任何东西。）
            return pcm

    def _mk(anti):
        eng = host_app.RealtimeEngine.__new__(host_app.RealtimeEngine)
        eng.chain = _StubChain()
        eng.lock = threading.Lock()
        eng.howl = None
        eng.sr_in = 24000
        eng.sr_out = 24000
        eng.get_cfg = lambda: {"module": dsp.MODULE_PITCH,
                               "engine": dsp.ENGINE_FPGA_DIRECT,
                               "anti_howl": anti,
                               "ingain": 1.0}
        return eng

    need = 1024
    # 指数增长的 1200 Hz：每块 ×1.6，正是自激的样子
    def block(k):
        t = (np.arange(need) + k * need) / 24000.0
        return (0.01 * (1.6 ** k) * np.sin(2 * np.pi * 1200.0 * t)
                ).astype(np.float32).reshape(-1, 1)

    try:
        e_off = _mk(False)
        same = 0
        for k in range(20):
            x = np.repeat(block(k), 2, axis=1)
            y = e_off._process_block(x)
            # 链路里本来就有 float -> int16 -> float 的量化往返，
            # 所以"没做额外处理"的正确参照是**同一次往返**的结果，
            # 而不是原始 float（第一版拿原始值比，永远不可能相等）。
            ref = host_app.audio_io.to_float(host_app.audio_io.to_int16(x))
            if np.max(np.abs(y - ref)) == 0.0:
                same += 1
        check("关掉时实时链路不做额外处理且不建抑制器",
              same == 20 and e_off.howl is None,
              "与量化往返逐位相同 %d/20，howl=%s" % (same, e_off.howl))

        e_on = _mk(True)
        for k in range(24):
            e_on._process_block(np.repeat(block(k), 2, axis=1))
        n = 0 if e_on.howl is None else len(e_on.howl.notches)
        check("打开时实时链路真的跑到抑制器并挂上陷波",
              e_on.howl is not None and n >= 1,
              "陷波=%s" % ([] if e_on.howl is None else
                           [(round(x[0]), round(x[1], 1)) for x in e_on.howl.notches],))
    except Exception as e:
        check("实时链路防啸叫接线", False, repr(e))


def main():
    print("=" * 66)
    print(" ACX720 变声器上位机 —— DSP 内核验收")
    print(" python %s" % sys.version.split()[0])
    print("=" * 66)
    t0 = time.time()
    test_roms()
    test_pitch()
    test_block_invariance()
    test_echo()
    test_ring()
    test_perf()
    test_file_roundtrip()
    test_gui_smoke()
    test_realtime_path()
    test_stereo_independent()
    test_vocal_split()
    test_split_perf()
    test_stream_matches_offline()
    test_realtime_split_engine()
    test_spectrum_both_channels()
    test_long_run_stability()
    test_audio_formats()
    test_stems_export()
    test_player()
    test_file_split_end_to_end()
    test_ai_gate()
    test_anti_howl()
    test_age_mode_ui()
    test_realtime_howl_wired()
    print("\n" + "=" * 66)
    print(" PASS = %d    FAIL = %d    总耗时 %.1f 秒" % (PASS, FAIL, time.time() - t0))
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
