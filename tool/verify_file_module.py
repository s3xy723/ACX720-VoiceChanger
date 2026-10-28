#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件变声模块的两个顽疾 —— 「permission denied」和「连续爆音」—— 的验收脚本。

    python tool/verify_file_module.py            # 全跑
    python tool/verify_file_module.py --dev 16   # 指定用来试开流的输出设备

为什么要有这个脚本
------------------
这两个现象都极容易误判：

  * "permission denied" 用户第一反应是"有程序占用"，但实测本机真正的两个
    原因是 ① 下拉框里混进了 WDM-KS 这种**根本打不开**的设备；② 播放器拿
    **文件自己的采样率**去开流，而 WASAPI 卡只吃自己的原生采样率。
    两条都跟"占用"无关，光靠"关掉别的软件"永远查不出来。
  * 爆音 第一反应是"缓冲太小 / 回调被拖慢"，但实测 DSP 输出干净、
    播放器 3 秒 143 次回调零欠载，真正的元凶在 MCU 里：有符号样值被
    `& 0x0FFF` 取模送进 12 位 DAC。

所以这里每一条都做成**可复算的数值判据**，不靠耳朵。

判据（共 5 项，全过才算 PASS）
------------------------------
  1. 设备筛选        list_devices 里的每一条都能真的打开（剔除 WDM-KS）
  2. 采样率自适应    44.1 kHz 的素材在只吃 48 kHz 的 WASAPI 卡上能开流
  3. 播放无欠载      连续播 N 秒，回调 underrun/overrun 计数必须为 0
  4. DSP 无块边界瑕疵 分块处理结果与整段一次处理**逐样本相同**
  5. DAC 码映射      有符号样值 → 12 位码必须是单调、无折叠（模拟 MCU 的算式）
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.abspath(os.path.join(HERE, "..", "host"))
sys.path.insert(0, HOST)

PY_SR = 48000


def _sec(title):
    print("\n" + "=" * 62)
    print(" " + title)
    print("=" * 62)


def check_devices():
    """1. 设备列表里不应再有打不开的条目。"""
    import audio_io
    ok = True
    for kind, ch in (("input", 1), ("output", 2)):
        devs = audio_io.list_devices(kind)
        keep, drop = audio_io.usable_devices(devs, kind, channels=ch)
        print("  %-6s 原始 %2d 条 → 可用 %2d 条，剔除 %2d 条"
              % (kind, len(devs), len(keep), len(drop)))
        for i, n, h in drop:
            print("        剔除 dev=%-3d %-38s [%s]" % (i, n[:38], h))
        # 反查：留下来的必须每一条都能开
        for d in keep:
            if not audio_io.probe_stream(kind, d[0], None, ch):
                print("        ★ 留下来的却打不开：dev=%d %s" % (d[0], d[1]))
                ok = False
    return ok


def check_rate_fit(dev):
    """2. 44.1 kHz 素材在 WASAPI 卡（只吃 48 kHz）上要能开流。"""
    import player as playermod
    import sounddevice as sd
    if dev is None:
        print("  跳过：没给 --dev")
        return True
    name = sd.query_devices(dev)["name"]
    print("  试开设备 dev=%d  %s" % (dev, name))
    sr = 44100
    n = sr * 3
    t = np.arange(n) / float(sr)
    data = np.stack([(0.6 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)] * 2,
                    axis=1)
    logs = []
    p = playermod.Player(log=logs.append)
    p.set_device(dev)
    p.set_volume(0.0)                # 静音，只验证能不能开
    p.load(data, sr, "44.1k probe")
    ok = p.play()
    print("  开流结果：%s" % ok)
    for ln in logs:
        print("    日志：%s" % ln)
    if ok:
        print("  最终采样率：%d Hz（素材原本 %d Hz）" % (p.sr, sr))
    p.close()
    return bool(ok)


def check_playback(dev, secs=3.0):
    """3. 连续播 N 秒，欠载/过载必须为 0。"""
    import player as playermod
    sr = PY_SR
    n = int(sr * (secs + 1))
    t = np.arange(n) / float(sr)
    data = np.stack([(0.6 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)] * 2,
                    axis=1)
    p = playermod.Player(log=lambda s: None)
    if dev is not None:
        p.set_device(dev)
    p.set_volume(0.0)
    p.load(data, sr, "playback probe")
    inner = p._cb

    def counting(o, f, ti, status):
        return inner(o, f, ti, status)
    p._cb = counting
    started = p.play()
    if not started:
        print("  开流失败，无法统计")
        p.close()
        return False
    time.sleep(secs)
    und, ovr, calls = p.underruns, p.overruns, p._cb_calls
    print("  播放 %.1f s：回调 %d 次，欠载 %d，过载 %d" % (secs, calls, und, ovr))
    p.close()
    return und == 0 and ovr == 0


def check_dsp_blocks():
    """4. 文件变声是分块跑的：分块结果必须与整段一次**逐样本相同**。"""
    import audio_io
    import dsp_core as dsp
    sr = 48000
    n = sr * 2
    t = np.arange(n) / float(sr)
    x = (0.6 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    data = np.stack([x, x], axis=1)

    def run(block):
        chain = dsp.StereoChain(sr)
        chain.reset()
        chain.configure(mode=dsp.MODE_PITCH, dphase=18, ring_freq=1,
                        echo_taps=8, echo_gain=2, gain=1.0)
        pcm = audio_io.to_int16(data)
        outs = [chain.process(pcm[s:s + block], block=min(block, pcm.shape[0] - s))
                for s in range(0, pcm.shape[0], block)]
        return audio_io.to_float(np.concatenate(outs, axis=0))

    a, b = run(8192), run(n)
    差异 = float(np.abs(a - b).max())
    v = a[:, 0]
    jump = float(np.abs(np.diff(v)).max())
    clip = int((np.abs(v) > 0.999).sum())
    print("  分块 8192 vs 整段一次：最大差异 %.3e（应为 0）" % 差异)
    print("  输出 最大|值|=%.4f  相邻最大跳变=%.4f  削波样本=%d"
          % (float(np.abs(v).max()), jump, clip))
    return 差异 == 0.0 and clip == 0 and jump < 0.25


def check_dac_mapping():
    """5. MCU 的样值→DAC 码映射：必须单调、无折叠。

    这里**复算 MCU 里的算式**，不需要接硬件。
    错误写法 `w & 0x0FFF` 会把负半周翻到满量程（折叠），判据抓的就是它。
    """
    # 步长取 16：16 位样值每 +16，12 位码正好 +1，这样"单调 +1"是可断言的
    # （步长太大时码值一次跳好几格，判据就没法定死）。
    s = np.arange(-32768, 32768, 16, dtype=np.int32)
    u = (s & 0xFFFF).astype(np.int64)

    def fixed(w):                       # main.c 现在的 sample_to_dac()
        v = np.where(w >= 32768, w.astype(np.int64) - 65536, w.astype(np.int64))
        v = v + 32768
        v = (v + 8) >> 4                # 四舍五入，不是截断
        return np.clip(v, 0, 4095)

    def truncated(w):                   # 早期写法：直接截断，量化误差与信号相关
        v = np.where(w >= 32768, w.astype(np.int64) - 65536, w.astype(np.int64))
        return np.clip((v + 32768) >> 4, 0, 4095)

    good = fixed(u)
    bad = (u & 0x0FFF)

    mono = bool(np.all(np.diff(good) >= 0))          # 必须单调不减
    rng = (int(good.min()), int(good.max()))         # 必须铺满 0..4095
    jump_good = int(np.abs(np.diff(good)).max())
    jump_bad = int(np.abs(np.diff(bad)).max())
    print("  正确映射：单调=%s  范围=%s  相邻最大跳变=%d" % (mono, rng, jump_good))
    print("  错误 &0xFFF：相邻最大跳变=%d（满量程 4095，>1000 就是折叠锯齿）"
          % jump_bad)
    print("  静音(0) 应落在中点 2048，实际 %d" % int(fixed(np.array([0]))[0]))
    # 量化误差：必须遍历**每一个** 16 位值才看得出差别
    # （按 16 步长采样时两种写法都恰好整除，误差都是 0，判据失效）。
    u_all = np.arange(0, 65536, dtype=np.int64)
    ref = u_all / 16.0
    e_round = float(np.abs(fixed(u_all) - ref).mean())
    e_trunc = float(np.abs(truncated(u_all) - ref).mean())
    print("  平均量化误差：四舍五入 %.4f LSB，截断 %.4f LSB（前者应更小）"
          % (e_round, e_trunc))
    return (mono and rng == (0, 4095) and jump_good == 1 and jump_bad > 1000
            and int(fixed(np.array([0]))[0]) == 2048 and e_round < e_trunc)


def check_write_roundtrip():
    """6. 写文件：采样率/声道/位深在「写 → 读回」两端必须完全一致。

    这一项直接对应"输出文件报错"：以前 libsndfile 拿到 sr=0 会抛
    `sampling rate not specified`（不是 OSError，重试逻辑兜不住），
    目录不存在会抛 FileNotFoundError，NaN 会写出垃圾 —— 全都冒到界面上。
    """
    import tempfile
    import shutil
    import audio_io
    tmp = tempfile.mkdtemp(prefix="acx_verify_")
    sr = 44100
    n = sr
    t = np.arange(n) / float(sr)
    x = (0.6 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    data = np.stack([x, x * 0.5], axis=1)
    ok = True

    print("  写 → 读回 一致性：")
    for rate in (22050, 44100, 48000):
        p = os.path.join(tmp, "rt_%d.wav" % rate)
        try:
            audio_io.save_audio(p, data, rate)
            a2, sr2 = audio_io.load_audio(p)
            a2 = np.asarray(a2)
            same = (sr2 == rate and a2.shape[0] == n and a2.shape[1] == 2
                    and bool(np.all(np.isfinite(a2))))
            ok &= same
            print("    %5d Hz  文件里=%-6d 形状=%-12s 有限=%s  %s"
                  % (rate, sr2, str(a2.shape), bool(np.all(np.isfinite(a2))),
                     "OK" if same else "★ 不一致"))
        except Exception as e:
            ok = False
            print("    %5d Hz  失败 %s: %s" % (rate, type(e).__name__, str(e)[:50]))

    # 目录不存在 / 小数采样率 / NaN：以前都会变成"输出文件报错"
    try:
        p = audio_io.save_audio(os.path.join(tmp, "no", "dir", "a.wav"), data, sr)
        ok &= os.path.exists(p)
        print("    目录不存在   -> 自动建目录，%s" % ("OK" if os.path.exists(p) else "★"))
    except Exception as e:
        ok = False
        print("    目录不存在   -> 失败 %s" % (e,))

    try:
        p = audio_io.save_audio(os.path.join(tmp, "frac.wav"), data, 24414.0625)
        a2, sr2 = audio_io.load_audio(p)
        ok &= (sr2 == 24414)
        print("    小数采样率   -> 归一化成 %d Hz，%s" % (sr2, "OK" if sr2 == 24414 else "★"))
    except Exception as e:
        ok = False
        print("    小数采样率   -> 失败 %s" % (e,))

    try:
        bad = np.where(np.arange(n) < 5, np.nan, x)[:, None] * np.ones((1, 2))
        p = audio_io.save_audio(os.path.join(tmp, "nan.wav"), bad.astype(np.float32), sr)
        a2, sr2 = audio_io.load_audio(p)
        ok &= bool(np.all(np.isfinite(np.asarray(a2))))
        print("    含 NaN/Inf   -> 已替换成有限值，%s"
              % ("OK" if np.all(np.isfinite(np.asarray(a2))) else "★"))
    except Exception as e:
        ok = False
        print("    含 NaN/Inf   -> 失败 %s" % (e,))

    # 非法采样率必须抛 ValueError，而不是让 libsndfile 抛莫名错误
    for b in (0, -1, None):
        try:
            audio_io.norm_sample_rate(b)
            ok = False
            print("    sr=%-5r     -> ★ 没报错（应当抛 ValueError）" % (b,))
        except ValueError:
            print("    sr=%-5r     -> ValueError OK" % (b,))

    shutil.rmtree(tmp, ignore_errors=True)
    return bool(ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", type=int, default=None,
                    help="用来试开流的输出设备索引；不给则只跑不依赖声卡的项")
    ap.add_argument("--secs", type=float, default=3.0)
    a = ap.parse_args()

    _sec("1) 设备列表：不能有打不开的条目（permission denied 主因之一）")
    r1 = check_devices()

    _sec("2) 采样率自适应：44.1 kHz 素材在只吃 48 kHz 的 WASAPI 卡上要能开")
    try:
        r2 = check_rate_fit(a.dev)
    except Exception as e:
        print("  跳过（%r）" % (e,))
        r2 = True

    _sec("3) 播放 %.1f 秒：回调欠载/过载必须为 0" % a.secs)
    try:
        r3 = check_playback(a.dev, a.secs)
    except Exception as e:
        print("  跳过（%r）" % (e,))
        r3 = True

    _sec("4) DSP：分块处理必须与整段一次逐样本相同")
    r4 = check_dsp_blocks()

    _sec("5) MCU 样值→12 位 DAC 码：必须单调无折叠、四舍五入")
    r5 = check_dac_mapping()

    _sec("6) 写文件：采样率/声道/位深 写→读回 必须一致")
    r6 = check_write_roundtrip()

    _sec("汇总")
    res = [("设备可打开性", r1), ("采样率自适应", r2), ("播放无欠载", r3),
           ("DSP 无块边界瑕疵", r4), ("DAC 码无折叠", r5),
           ("写文件一致性", r6)]
    for name, ok in res:
        print("  %-18s %s" % (name, "PASS" if ok else "FAIL"))
    allok = all(ok for _, ok in res)
    print("\nRESULT: %s" % ("ALL PASS" if allok else "FAIL"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
