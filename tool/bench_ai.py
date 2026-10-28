# -*- coding: utf-8 -*-
"""在合成二重唱真值上对比三种方案。"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "host")))

import bench_split as B
import audio_io
import dsp_core as dsp
import vocal_route

mix, gt = B.make_duet()
sr = B.SR
n = len(mix)
print("基准 %.0f 秒 @ %d Hz" % (n / sr, sr))


def score(tag, out_L, out_R, male, female, acc, secs):
    print("\n=== %s（%.1f s）===" % (tag, secs))
    m = np.minimum(len(male), n)
    r = B.evaluate(out_L, out_R, {"male": male, "female": female, "accomp": acc}, gt)
    return r


def assemble(acc, male, female):
    y = np.empty((len(male), 2), dtype=np.float64)
    y[:, 0] = acc[:, 0] + male
    y[:, 1] = acc[:, 1] + female
    return y


# ---------------- A) demucs 人声 + 自相关路由 ----------------
import ai_separate
ok, why = ai_separate.available()
if not ok:
    print("demucs 不可用:", why)
    sys.exit(1)

t0 = time.time()
voc, acc = ai_separate.separate(mix, sr, progress=lambda s: print("    ", s, flush=True))
t_ai = time.time() - t0
voc_m = 0.5 * (voc[:, 0] + voc[:, 1])
print("demucs 人声 %.1f dB / 伴奏 %.1f dB（%.1f s）"
      % (B.si_sdr(voc[:, 0], 0.55 * (gt["male"] + gt["female"])),
         B.si_sdr(acc[:, 0], 0.9 * gt["acc_l"]), t_ai))

st = {}
t1 = time.time()
male, female = vocal_route.route_vocal(voc_m, sr, split=165.0, depth=1.0, stats=st)
t_rt = time.time() - t1
print("路由 %.1f s  统计: %s" % (t_rt, {k: round(v, 1) for k, v in st.items()}))
y = assemble(acc.astype(np.float64), male.astype(np.float64), female.astype(np.float64))
score("demucs + 自相关路由", y[:, 0], y[:, 1], male, female, acc, t_ai + t_rt)

# 分段落看
print("\n  分段（男独唱 8-24 / 女独唱 24-40 / 齐唱 40-56 / 齐唱 64-72）:")
for tag, a, b in (("男独唱", 8, 24), ("女独唱", 24, 40), ("齐唱", 40, 56), ("齐唱", 64, 72)):
    sl = slice(a * sr, min(n, b * sr))
    print("    %s: 男轨 SI-SDR %+6.2f dB   女轨 %+6.2f dB"
          % (tag,
             B.si_sdr(male[sl], (gt["male"] * 0.55)[sl]),
             B.si_sdr(female[sl], (gt["female"] * 0.55)[sl])))

# ---------------- B) 纯 DSP 中置提取 + 同一套路由 ----------------
t0 = time.time()
sp = dsp.VocalSplitter(sr)
sp.configure(depth=1.0)
_v, _st = sp.process(mix, stems=True)
# 中置提取出来的"人声" = 男+女+居中伴奏，把它也交给自相关路由
center = 0.5 * (_st["male"] + _st["female"]) if False else None
# 直接用 splitter 的男轨当中心估计不可靠，这里改成单独跑一遍中置提取
from vocal_split import NFFT, HOP, _WIN
mid = 0.5 * (mix[:, 0] + mix[:, 1])
side = 0.5 * (mix[:, 0] - mix[:, 1])
nf = (n - NFFT) // HOP
cen = np.zeros(n)
cnt = np.zeros(n)
for k in range(nf):
    s = k * HOP
    M = np.fft.rfft(mid[s:s + NFFT] * _WIN)
    Sd = np.fft.rfft(side[s:s + NFFT] * _WIN)
    c = np.clip((np.abs(M) - np.abs(Sd)) / (np.abs(M) + 1e-12), 0, 1)
    cen[s:s + NFFT] += np.fft.irfft(c * M, NFFT) * _WIN * (2.0 / 3.0)
    cnt[s:s + NFFT] += _WIN * _WIN
cen /= np.maximum(cnt, 1e-9)

male2, female2 = vocal_route.route_vocal(cen, sr, split=165.0, depth=1.0)
y2 = np.empty((n, 2))
y2[:, 0] = mix[:, 0] - female2
y2[:, 1] = mix[:, 1] - male2
score("纯 DSP 中置提取 + 自相关路由", y2[:, 0], y2[:, 1], male2, female2,
      np.stack([mix[:, 0] - male2 - female2, mix[:, 1] - male2 - female2], axis=1),
      time.time() - t0)

if len(sys.argv) > 1:
    out = sys.argv[1]
    audio_io.save_audio(out, y.astype(np.float32), sr)
    base = os.path.splitext(out)[0]
    audio_io.save_audio(base + "_男声.wav", male, sr)
    audio_io.save_audio(base + "_女声.wav", female, sr)
    audio_io.save_audio(base + "_伴奏.wav", acc, sr)
    print("\n已写出", out)
