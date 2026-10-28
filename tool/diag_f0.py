# -*- coding: utf-8 -*-
"""看基频跟踪到底测出了什么：验证"八度错误导致男声被判成女声"的猜想。"""
import sys, os
import numpy as np
sys.path.insert(0, r'C:\Users\s3xy\WorkBuddy\2026-10-10-21-21-17\acx720_voice_changer\host')
import audio_io
from vocal_split import VocalSplitter, NFFT, HOP, _WIN

P = r'C:\Users\s3xy\Downloads\老狼&王婧_想把我唱给你听.flac'
a, sr = audio_io.load_audio(P, {})
x = a.astype(np.float64)
L, R = x[:, 0], x[:, 1]
mid = 0.5 * (L + R)

sp = VocalSplitter(sr)
sp.configure()
n = len(x)
nfr = (n - NFFT) // HOP
f0s = np.zeros(nfr, dtype=np.float32)
confs = np.zeros(nfr, dtype=np.float32)
for k in range(nfr):
    s = k * HOP
    seg = mid[s:s + NFFT] * _WIN
    # 带通到人声基频范围再估
    S = np.fft.rfft(seg)
    f = np.fft.rfftfreq(NFFT, 1.0 / sr)
    S[(f < 45) | (f > 1200)] = 0
    w = np.fft.irfft(S, NFFT)
    f0, c = sp._estimate_f0(w)
    f0s[k], confs[k] = f0, c

good = (confs > 0.35) & (f0s > 60) & (f0s < 600)
v = f0s[good]
print('有效帧 %d / %d' % (good.sum(), nfr))
print('F0 直方图（每个 bin 20 Hz）：')
h, e = np.histogram(v, bins=np.arange(60, 601, 20))
for i in range(len(h)):
    if h[i] > 0:
        print('  %3d-%3d Hz  %5d  %s' % (e[i], e[i + 1], h[i], '#' * int(60 * h[i] / h.max())))
print('中位数 %.0f Hz  10%%=%.0f  90%%=%.0f' % (np.median(v), np.percentile(v, 10), np.percentile(v, 90)))

print('\n逐 5 秒的 F0 中位数（应该能看到两位歌手的音域区间）：')
for s in range(0, nfr, int(5 * sr / HOP)):
    e2 = min(nfr, s + int(5 * sr / HOP))
    seg = f0s[s:e2][good[s:e2]]
    if len(seg) < 8:
        print('  %4d s  (无有效帧)' % (s * HOP // sr))
        continue
    print('  %4d s  中位=%6.1f Hz   低段(60-180)=%4.0f%%  高段(180-400)=%4.0f%%'
          % (s * HOP // sr, np.median(seg),
             100.0 * ((seg < 180).mean()), 100.0 * ((seg >= 180) & (seg < 400)).mean()))
