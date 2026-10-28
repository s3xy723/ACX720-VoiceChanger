#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只测 PC 侧：CABLE Output → StreamResampler → 24.414 kHz，存 wav + 打谱。

目的：把"杂音"到底出在 PC 侧（采集/重采样）还是硬件侧（FPGA→MCU→DAC）分开。
本脚本**完全不碰串口/板子**。

    python tool/probe_bridge_dsp.py [freq] [amp]
"""
import os
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fpga_audio_bridge import StreamResampler, FS_OUT  # noqa: E402


def pick(pred, kind):
    for i, d in enumerate(sd.query_devices()):
        if d.get(kind, 0) > 0 and pred(d.get("name", "")):
            return i, d
    return None, None


def main():
    F = float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0
    AMP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5

    cap, cap_name = pick(lambda n: "CABLE Output" in n, "max_input_channels")
    out, out_name = pick(lambda n: "CABLE Input" in n, "max_output_channels")
    d = sd.query_devices(cap)
    fs_in = int(d["default_samplerate"])
    print("采集 %d %s @ %d Hz\n播放 %d %s\n" % (cap, cap_name, fs_in, out, out_name))

    rs = StreamResampler(fs_in, FS_OUT)
    got = []
    stop = threading.Event()

    def cb(indata, frames, tinfo, status):
        if status:
            print("status:", status)
        y = rs.push(np.array(indata, dtype=np.float64, copy=True))
        if len(y):
            got.append(y[:, 0].copy())

    st = sd.InputStream(device=cap, channels=2, samplerate=fs_in,
                        blocksize=max(256, int(fs_in * 0.01)),
                        dtype="float32", callback=cb)
    st.start()
    time.sleep(0.5)

    n = int(5.0 * 48000)
    t = np.arange(n) / 48000.0
    v = (AMP * np.sin(2 * np.pi * F * t)).astype(np.float32)
    v[:4800] = 0.0
    with sd.OutputStream(samplerate=48000, channels=2, dtype="float32",
                         device=out, blocksize=2048) as o:
        o.write(np.stack([v, v], axis=1))
    time.sleep(0.5)
    st.stop()
    st.close()

    y = np.concatenate(got) if got else np.zeros(1)
    i0 = int(1.0 * FS_OUT)
    seg = y[i0:i0 + int(4.0 * FS_OUT)].astype(np.float64)
    seg -= seg.mean()
    w = np.hanning(seg.size)
    V = np.abs(np.fft.rfft(seg * w)) / (np.sum(w) / 2.0)
    fr = np.fft.rfftfreq(seg.size, 1.0 / FS_OUT)
    db = 10 * np.log10(V ** 2 + 1e-20)
    print("输出 %d 点 @ %.1f Hz，RMS %.1f dBFS，peak %.1f dBFS" %
          (seg.size, FS_OUT, 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12),
           20 * np.log10(np.abs(seg).max() + 1e-12)))

    def bp(f0, bw=30.0):
        m = np.abs(fr - f0) < bw
        return 10 * np.log10((V[m] ** 2).sum() + 1e-20)

    base = bp(F)
    print("\nf0=%.0f Hz  %+.1f dBFS" % (F, base))
    tot = 0.0
    for k in range(2, 9):
        if F * k > FS_OUT / 2:
            break
        h = bp(F * k) - base
        tot += 10 ** (h / 10)
        print("  H%d %6.0f Hz  %+6.1f dB" % (k, F * k, h))
    print("  THD(2~8) %+6.1f dB" % (10 * np.log10(tot + 1e-20)))

    print("\n--- 分带（相对最强峰）---")
    ref = db.max()
    for lo in range(0, 12000, 1000):
        m = (fr >= lo) & (fr < lo + 1000)
        e = 10 * np.log10((V[m] ** 2).sum() + 1e-20) - ref
        print("  %5d-%5d  %+6.1f dB  %s" % (lo, lo + 1000, e, "#" * max(0, int((e + 70) / 2))))

    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    p = os.path.join(HERE, "logs", "bridge_dsp_%d.wav" % int(F))
    with wave.open(p, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(round(FS_OUT)))
        f.writeframes((np.clip(seg, -1, 1) * 32767).astype("<i2").tobytes())
    print("\n已保存 %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
