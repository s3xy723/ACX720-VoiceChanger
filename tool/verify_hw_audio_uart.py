#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""硬件音频链路测试：发**门控**测试音，同时录音，用 ON/OFF 窗判有没有声。

    PC -> COM4(1.5Mbaud) -> FPGA -> SPI -> STM32 -> DAC -> 功放 -> 喇叭
                                                                  ~> 麦克风 -> PC

为什么用门控（ON/OFF 交替）而不是"发一段看涨了多少"
------------------------------------------------
房间里有人声/环境噪声时，"发声期间 vs 之前静默"的对比会被噪声淹没：
实测同一套代码两次，一次 +17.9 dB（那次屋里安静），一次 +2.3 dB（屋里有人声），
结论完全相反。所以改成：

    0.7 s 发 + 0.7 s 停，重复 4 轮，全程连续录音
    → ON 窗与 OFF 窗处在**同一段录音的相邻时间里**，环境噪声对两者相同
    → 判据 = ON 窗电平 − OFF 窗电平，噪声抵消，只剩链路自己的贡献

再用两个"没发"的频点（1.5×freq、250 Hz）当特异性对照：
如果 ON/OFF 差在那两个频点上也一样大，说明测到的是串口活动带来的电气噪声，不是声音。
"""
import argparse
import os
import sys
import threading
import time

import numpy as np
import serial
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from uart_frame import BAUD, FS_OUT, N_SAMPLES, build_frame   # noqa: E402

MIC_SR = 48000
PRE_ROLL = 1.0          # 录音先跑 1 s 再开始发
ON_SEC = 0.7
OFF_SEC = 0.7
CYCLES = 4


def _pick_mic():
    try:
        d_in, _ = sd.default.device
    except Exception:
        d_in = None
    devs = sd.query_devices()
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0 and "麦克风" in d.get("name", ""):
            return i, d.get("name")
    if isinstance(d_in, int) and d_in >= 0:
        return d_in, devs[d_in].get("name", "?")
    return None, None


def _level_at(buf, sr, freq):
    """单频点幅度（dBFS），Hann 窗 + 单点 DFT。"""
    buf = np.asarray(buf, dtype=np.float64)
    if buf.size < 256:
        return -120.0
    x = buf - buf.mean()
    w = np.hanning(buf.size)
    k = np.arange(buf.size)
    z = np.sum(x * w * np.exp(-1j * 2 * np.pi * freq / sr * k))
    return 20.0 * np.log10(abs(z) * 2.0 / max(1e-9, np.sum(w)) + 1e-12)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=float, default=1000.0)
    ap.add_argument("--amp", type=float, default=0.9)
    ap.add_argument("--port", default=None,
                    help="FPGA 的 CH340 串口号。默认先试 COM4，"
                         "打不开时自动改用机器上唯一的 CH340")
    a = ap.parse_args()

    # CH340 的串口号会随插拔漂移（这次是 COM5，上次是 COM4），写死就会
    # "看起来在发、板子毫无反应"。先按给定/默认试，打不开就扫一遍现有的。
    port = a.port
    if port is None:
        try:
            from serial.tools import list_ports
            ch340 = [p.device for p in list_ports.comports()
                     if "1A86" in (p.hwid or "").upper()
                     or "CH340" in (p.description or "").upper()
                     or "CH340" in (p.device or "")]
            if len(ch340) == 1:
                port = ch340[0]
        except Exception:
            pass
    if port is None:
        port = "COM4"

    mic, mic_name = _pick_mic()
    print("麦克风：idx=%s  %s" % (mic, mic_name))
    if mic is None:
        print("RESULT: INCONCLUSIVE 没有可用输入设备")
        return 2

    rec = {"buf": []}
    stop = threading.Event()

    def _cap():
        with sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32",
                            device=mic, blocksize=2048) as st:
            while not stop.is_set():
                b, _ = st.read(2048)
                rec["buf"].append(b[:, 0])

    th = threading.Thread(target=_cap, daemon=True)
    th.start()
    time.sleep(PRE_ROLL)

    try:
        ser = serial.Serial(port, BAUD, timeout=0.2, write_timeout=3.0)
    except Exception as e:
        stop.set(); th.join(timeout=2)
        print("RESULT: FAIL 打不开 %s：%r" % (port, e))
        return 1
    print("串口：%s @ %d 8N1  —— 帧格式 %d 采样/帧、%.4f kHz、%d 字节/帧"
          % (port, BAUD, N_SAMPLES, FS_OUT / 1000.0, 261))

    # ---------------- 门控发送 ----------------
    fps = FS_OUT / float(N_SAMPLES)
    marks = []                      # (start_sec, end_sec, on?)
    t0 = time.time()
    idx0 = 0
    sent = 0
    try:
        for c in range(CYCLES):
            for on in (True, False):
                s = time.time() - t0
                n_fr = int((ON_SEC if on else OFF_SEC) * fps)
                for _ in range(n_fr):
                    n = np.arange(idx0, idx0 + N_SAMPLES)
                    idx0 += N_SAMPLES
                    tt = n / float(FS_OUT)
                    v = (a.amp * np.sin(2 * np.pi * a.freq * tt)) if on else np.zeros(N_SAMPLES)
                    l = (v * 32767.0).astype("<i2")
                    ser.write(build_frame(np.stack([l, l], axis=1), sent & 0xFF))
                    sent += 1
                    want = sent / fps
                    dt = want - (time.time() - t0)
                    if dt > 0:
                        time.sleep(min(dt, 0.05))
                marks.append((s, time.time() - t0, on))
        ser.close()
    except Exception as e:
        print("发送中断：%r" % (e,))
    print("已发帧数 %d（%.1f s）" % (sent, time.time() - t0))

    time.sleep(0.4)
    stop.set(); th.join(timeout=3.0)
    y = np.concatenate(rec["buf"]) if rec["buf"] else np.zeros(0)
    print("录音 %.1f s" % (y.size / float(MIC_SR)))

    # ---------------- ON/OFF 窗分析 ----------------
    def win_lvl(freq, on):
        vals = []
        for s, e, flag in marks:
            if flag is not on:
                continue
            mid = (s + e) / 2.0 + PRE_ROLL
            i0 = int((mid - 0.25) * MIC_SR)
            i1 = int((mid + 0.25) * MIC_SR)
            if 0 <= i0 < i1 <= y.size:
                vals.append(_level_at(y[i0:i1], MIC_SR, freq))
        return float(np.mean(vals)) if vals else -120.0

    freqs = [a.freq, a.freq * 1.5, a.freq / 9.0, a.freq * 2.0 / 9.0, 250.0]
    print("\n%-10s %10s %10s %10s" % ("频点", "ON 窗", "OFF 窗", "ON−OFF"))
    res = {}
    for f in freqs:
        on_l = win_lvl(f, True)
        off_l = win_lvl(f, False)
        res[f] = on_l - off_l
        tag = "（所发频点）" if abs(f - a.freq) < 1.0 else "（对照，没发）"
        print("%-10.0f %9.1fd %9.1fd %9.1f dB  %s" % (f, on_l, off_l, res[f], tag))

    d_main = res[a.freq]
    d_ctrl = max(res[f] for f in freqs if abs(f - a.freq) > 1.0)
    print("\n判定：主频点提升 %+.1f dB，对照频点最大提升 %+.1f dB" % (d_main, d_ctrl))
    if d_main > 12.0 and d_main - d_ctrl > 8.0:
        print("RESULT: PASS 喇叭出声，且只在我们发的那个频点上（链路闭环）")
        return 0
    if d_main <= 12.0:
        print("RESULT: FAIL 喇叭没出声（ON/OFF 差仅 %+.1f dB）" % d_main)
        return 1
    print("RESULT: INCONCLUSIVE 有提升但对照频点也在涨（%.1f dB），"
          "可能是串口活动带来的电气噪声，不是声音" % d_ctrl)
    return 1


if __name__ == "__main__":
    sys.exit(main())
