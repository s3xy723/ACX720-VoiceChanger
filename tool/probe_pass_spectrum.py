#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录一段 DUT 输出，打印频谱峰值 + 200 Hz 分带能量 + 存 wav。

    python tool/probe_pass_spectrum.py [freq] [amp]
"""
import os
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
MIC, MIC_SR = 1, 48000
LOG = os.path.join(HERE, "logs")


def pick(pred, kind):
    for i, d in enumerate(sd.query_devices()):
        if d.get(kind, 0) > 0 and pred(d.get("name", "")):
            return i
    return None


def main():
    F = float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0
    AMP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    cab = pick(lambda n: "CABLE Input" in n, "max_output_channels")

    br = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "fpga_audio_bridge.py")],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(2.5)

    rec, stop = [], threading.Event()

    def cap():
        with sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32",
                            device=MIC, blocksize=1024) as s:
            while not stop.is_set():
                b, _ = s.read(1024)
                rec.append(b[:, 0].copy())

    th = threading.Thread(target=cap, daemon=True)
    th.start()
    time.sleep(0.5)

    n = int(5.0 * 48000)
    t = np.arange(n) / 48000.0
    v = (AMP * np.sin(2 * np.pi * F * t)).astype(np.float32)
    v[:2400] = 0.0
    with sd.OutputStream(samplerate=48000, channels=2, dtype="float32",
                         device=cab, blocksize=2048) as out:
        out.write(np.stack([v, v], axis=1))
    time.sleep(0.6)
    stop.set()
    th.join(timeout=3)
    br.terminate()

    y = np.concatenate(rec) if rec else np.zeros(1)
    i0 = int(1.0 * MIC_SR)
    seg = y[i0:i0 + int(4.0 * MIC_SR)].astype(np.float64)
    seg = seg - seg.mean()
    w = np.hanning(seg.size)
    V = np.abs(np.fft.rfft(seg * w)) / (np.sum(w) / 2.0)
    fr = np.fft.rfftfreq(seg.size, 1.0 / MIC_SR)
    top = 10 * np.log10(V ** 2 + 1e-20)

    print("录音 %d 点  RMS %.1f dBFS  peak %.1f dBFS" %
          (seg.size, 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12),
           20 * np.log10(np.abs(seg).max() + 1e-12)))
    print("\n--- 前 18 个谱峰（相对最强峰） ---")
    idx = np.argsort(V)[::-1]
    shown, used = 0, np.zeros(fr.size, bool)
    for i in idx:
        if used[max(0, i - 40):i + 41].any():
            continue
        used[max(0, i - 40):i + 41] = True
        print("  %8.1f Hz   %+6.1f dB   (%.1f dBFS)" %
              (fr[i], top[i] - top[idx[0]], top[i]))
        shown += 1
        if shown >= 18:
            break

    print("\n--- 200 Hz 分带能量（相对最强峰） ---")
    ref = top[idx[0]]
    for lo in range(0, 24000, 1000):
        m = (fr >= lo) & (fr < lo + 1000)
        if m.any():
            e = 10 * np.log10((V[m] ** 2).sum() + 1e-20) - ref
            bar = "#" * max(0, int((e + 60) / 2))
            print("  %5d-%5d Hz  %+6.1f dB  %s" % (lo, lo + 1000, e, bar))

    os.makedirs(LOG, exist_ok=True)
    p = os.path.join(LOG, "pass_%d.wav" % int(F))
    with wave.open(p, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(MIC_SR)
        f.writeframes((np.clip(seg, -1, 1) * 32767).astype("<i2").tobytes())
    print("\n已保存 %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
