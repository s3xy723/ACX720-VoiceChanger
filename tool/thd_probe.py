#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测喇叭输出的 THD（总谐波失真），用来给「模拟段干不干净」一个数字。

    python tool/thd_probe.py            录 4 秒，量 440 Hz 的 2~7 次谐波
    python tool/thd_probe.py 880        量别的基频

前提条件：MCU 里跑的是只放内置正弦的诊断固件（-DTONE_ONLY），
也就是**完全不接 FPGA 数据** —— 这样测到的失真只可能来自
DAC → RC → 功放 → 喇叭，跟 PC 侧、跟 FPGA 都无关。

参考基准：本机 PC 扬声器（Realtek）用同样方法量出来是 **-45 dB** 量级。
"""
import sys
import threading
import time

import numpy as np
import sounddevice as sd

MIC, MIC_SR = 1, 48000
F = float(sys.argv[1]) if len(sys.argv) > 1 else 440.0


def main():
    rec, stop = [], threading.Event()

    def cap():
        with sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32",
                            device=MIC, blocksize=2048) as s:
            while not stop.is_set():
                b, _ = s.read(2048)
                rec.append(b[:, 0])

    th = threading.Thread(target=cap, daemon=True)
    th.start()
    time.sleep(1.2)
    time.sleep(4.0)
    stop.set()
    th.join(timeout=3)
    y = np.concatenate(rec)

    NW = int(1.0 * MIC_SR)
    seg = y[int(1.5 * MIC_SR):int(1.5 * MIC_SR) + NW]
    if seg.size < NW:
        print("录音太短"); return 1
    V = np.abs(np.fft.rfft((seg - seg.mean()) * np.hanning(seg.size)))
    fr = np.fft.rfftfreq(seg.size, 1.0 / MIC_SR)

    def band(f, w=10.0):
        m = np.abs(fr - f) < w
        return float((V[m] ** 2).sum())

    base = band(F)
    rms = 20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-12))
    harm = 0.0
    parts = []
    for k in (2, 3, 4, 5, 6, 7):
        h = band(F * k)
        harm += h
        parts.append("%d次%+.0f" % (k, 10 * np.log10(max(h, 1e-30) / max(base, 1e-30))))
    thd = 10 * np.log10(max(harm, 1e-30) / max(base, 1e-30))
    print("  基频 %.0f Hz  RMS %.1f dBFS | %s | THD %+.1f dB"
          % (F, rms, " ".join(parts), thd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
