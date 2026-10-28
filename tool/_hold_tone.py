#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只往 CABLE Input 灌一段正弦，不管板子（配合已经在跑的桥接程序用）。

    python tool/_hold_tone.py [freq] [amp] [secs]

用途：诊断时需要一个"一直有声音"的信号源，后台开着即可。
"""
import sys

import numpy as np
import sounddevice as sd

F = float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0
AMP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.6
SEC = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

dev = None
for i, d in enumerate(sd.query_devices()):
    if d["max_output_channels"] > 0 and "CABLE Input" in d["name"]:
        dev = i
        break
if dev is None:
    print("没有 CABLE Input")
    sys.exit(2)

sr = 48000
n = int(SEC * sr)
t = np.arange(n) / float(sr)
v = (AMP * np.sin(2 * np.pi * F * t)).astype(np.float32)
with sd.OutputStream(samplerate=sr, channels=2, dtype="float32",
                     device=dev, blocksize=2048) as o:
    o.write(np.stack([v, v], axis=1))
print("played %.1f s @ %.0f Hz amp %.2f" % (SEC, F, AMP))
