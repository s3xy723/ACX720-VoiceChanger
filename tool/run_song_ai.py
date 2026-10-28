# -*- coding: utf-8 -*-
"""把用户那首二重唱走完整的 AI 流水线，输出可试听的文件。"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "host")))

import audio_io
import ai_separate
import vocal_route

SRC = r"C:\Users\s3xy\Downloads\老狼&王婧_想把我唱给你听.flac"
OUT = r"C:\Users\s3xy\Downloads\想把我唱给你听_男左女右.wav"

t0 = time.time()
x, sr = audio_io.load_audio(SRC, {})
print("载入 %s  %.1f s @ %d Hz" % (x.shape, len(x) / sr, sr), flush=True)

voc, acc = ai_separate.separate(x, sr, progress=lambda s: print("   ", s, flush=True))
print("demucs 完成，用时 %.1f s" % (time.time() - t0), flush=True)

vm = 0.5 * (voc[:, 0] + voc[:, 1])
st = {}
t1 = time.time()
male, female = vocal_route.route_vocal(vm, sr, split=165.0, depth=1.0, stats=st,
                                       progress=lambda k, n: None)
print("路由完成 %.1f s  统计 %s" % (time.time() - t1,
                                    {k: round(v, 1) for k, v in st.items()}), flush=True)

y = np.empty_like(acc)
y[:, 0] = acc[:, 0] + male
y[:, 1] = acc[:, 1] + female
audio_io.save_audio(OUT, y, sr)
base = os.path.splitext(OUT)[0]
audio_io.save_audio(base + "_男声.wav", male, sr)
audio_io.save_audio(base + "_女声.wav", female, sr)
audio_io.save_audio(base + "_伴奏.wav", acc, sr)
print("写出：\n  %s\n  %s_男声.wav\n  %s_女声.wav\n  %s_伴奏.wav"
      % (OUT, base, base, base))
print("总用时 %.1f s（%.1f 倍实时）" % (time.time() - t0, (len(x) / sr) / (time.time() - t0)))
