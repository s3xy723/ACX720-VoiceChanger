# -*- coding: utf-8 -*-
"""在干净人声上对比几种基频估计：HPS vs 自相关 vs 自相关+次谐波累加。"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "host")))
import dsp_core as dsp
from vocal_split import NFFT, HOP, _WIN

voc = np.load(os.path.join(HERE, "_demucs_voc.npy"))
sr = 44100
v = 0.5 * (voc[:, 0] + voc[:, 1])
sp = dsp.VocalSplitter(sr)
sp.configure()

F0WIN = 2048
WIN2 = np.hanning(F0WIN + 1)[:F0WIN]


def f0_acf(w, sr, fmin=70.0, fmax=500.0, K=1):
    """归一化自相关；K>1 时做次谐波累加（真周期在 τ,2τ,3τ… 都有峰）。"""
    w = (w - w.mean()) * WIN2[:len(w)]
    n = len(w)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    S = np.fft.rfft(w, nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    S[(f < fmin * 0.55) | (f > 3000)] = 0
    acf = np.fft.irfft(S * np.conj(S), nfft)[:n]
    acf /= (acf[0] + 1e-12)
    lo = max(2, int(sr / (fmax * 1.15)))
    hi = min(n - 1, int(sr / (fmin * 0.9)))
    if hi <= lo + 2:
        return 0.0, 0.0
    best, bl = -9.0, -1
    for lag in range(lo, hi + 1):
        s = 0.0
        for k in range(1, K + 1):
            j = k * lag
            if j >= n:
                break
            s += acf[j] / k
        if s > best:
            best, bl = s, lag
    return (sr / bl if bl > 0 else 0.0), best


for tag, t0, t1 in (("男声独唱", 1.0, 7.0), ("女声独唱", 16.0, 23.0), ("齐唱", 8.0, 15.0)):
    print("\n===== %s =====" % tag)
    for K in (1, 5):
        vals = []
        for tt in np.arange(t0, t1, 0.2):
            k = int(tt * sr / HOP)
            end = k * HOP + HOP
            st = end - F0WIN
            if st < 0 or end > len(v):
                continue
            f0, c = f0_acf(v[st:end], sr, K=K)
            if 70 < f0 < 500:
                vals.append(f0)
        vals = np.array(vals)
        if len(vals) == 0:
            continue
        h, e = np.histogram(vals, bins=np.arange(70, 510, 20))
        top = np.argsort(h)[-4:][::-1]
        print("  acf K=%d  中位 %6.1f Hz  主要: %s" % (
            K, np.median(vals), '  '.join('%d-%d:%d' % (e[i], e[i+1], h[i]) for i in top)))

    # HPS 对照
    vals = []
    for tt in np.arange(t0, t1, 0.2):
        k = int(tt * sr / HOP)
        seg = v[k * HOP:k * HOP + NFFT]
        if len(seg) < NFFT:
            continue
        sal = sp._salience(np.abs(np.fft.rfft(seg * _WIN)))
        vals.append(sp._cands[int(np.argmax(sal))])
    vals = np.array(vals)
    h, e = np.histogram(vals, bins=np.arange(70, 510, 20))
    top = np.argsort(h)[-4:][::-1]
    print("  HPS      中位 %6.1f Hz  主要: %s" % (
        np.median(vals), '  '.join('%d-%d:%d' % (e[i], e[i+1], h[i]) for i in top)))
