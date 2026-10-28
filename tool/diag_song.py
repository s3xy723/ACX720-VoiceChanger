# -*- coding: utf-8 -*-
"""诊断：现有分离算法在这首二重唱上的实际表现。"""
import sys, os, time
import numpy as np
sys.path.insert(0, r'C:\Users\s3xy\WorkBuddy\2026-10-10-21-21-17\acx720_voice_changer\host')
import audio_io, dsp_core as dsp

P = r'C:\Users\s3xy\Downloads\老狼&王婧_想把我唱给你听.flac'
FS_TARGET = 44100

a, sr = audio_io.load_audio(P, {})
print('载入 %s  %.1f s @ %d Hz' % (a.shape, a.shape[0] / sr, sr))
x = a.astype(np.float32)

sp = dsp.VocalSplitter(sr)
sp.configure(depth=1.0)
t0 = time.time()
y, st = sp.process(x, block=8192, stems=True)
dt = time.time() - t0
print('处理耗时 %.1f s（%.0f 倍实时）' % (dt, (len(x) / sr) / dt))
info = sp.summary()
print('统计：男 %.1f%% 女 %.1f%% 未判定 %.1f%%  分界 %.0f Hz  单声道=%s'
      % (info['male_pct'], info['female_pct'], info['unvoiced_pct'],
         info['thr_hz'], info['mono']))

# ---- 每 20 秒一段，看男女判定怎么跳 ----
seg = 20 * sr
print('\n每 20 秒的性别构成：')
print('  时间     男%%   女%%   未判定%%  男轨RMS  女轨RMS  L-R平均差')
for s in range(0, len(x) - seg, seg):
    e = s + seg
    # 用音轨能量反推：男轨能量高说明这一段被判为男
    m = float(np.sqrt((st['male'][s:e] ** 2).mean()))
    f = float(np.sqrt((st['female'][s:e] ** 2).mean()))
    # 统计判定：逐 256 点看哪条轨强
    nb = seg // 256
    mseg = st['male'][s:s + nb * 256].reshape(nb, 256)
    fseg = st['female'][s:s + nb * 256].reshape(nb, 256)
    em = (mseg ** 2).mean(axis=1)
    ef = (fseg ** 2).mean(axis=1)
    vo = np.maximum(em, ef) > 1e-8
    male_pct = 100.0 * np.mean(em[vo] > ef[vo]) if vo.any() else 0
    unv = 100.0 * (1 - vo.mean())
    d = float(np.abs(y[s:e, 0] - y[s:e, 1]).mean())
    print('  %4d s  %5.1f  %5.1f  %6.1f   %.4f   %.4f   %.4f'
          % (s // sr, male_pct, 100 - male_pct - unv, unv, m, f, d))

# ---- 伴奏有没有被动过：找"人声很弱"的片段 ----
print('\n伴奏保真度检查（取人声能量最低的 6 段，各 4 秒）：')
win = 4 * sr
ve = np.array([np.sqrt((st['male'][s:s + win] ** 2).mean()
                       + (st['female'][s:s + win] ** 2).mean())
               for s in range(0, len(x) - win, win)])
order = np.argsort(ve)
for i in order[:6]:
    s = int(i) * win
    d = float(np.abs(y[s:s + win] - x[s:s + win]).max())
    print('  %4d s  人声能量=%.5f  输出与原混音 max|diff|=%.4f' % (s // sr, ve[i], d))

# ---- 人声段里，左右声道还像不像 ----
print('\n人声段的声道去相关（越负越好，说明人声被拉开了）：')
voiced = np.maximum(st['male'], st['female'])
thr = np.percentile(voiced, 80)
mask = voiced > thr
for s in range(0, len(x) - 8 * sr, 8 * sr):
    e = s + 8 * sr
    mk = mask[s:e]
    if mk.mean() < 0.15:
        continue
    c_in = float(np.corrcoef(x[s:e][mk, 0], x[s:e][mk, 1])[0, 1])
    c_out = float(np.corrcoef(y[s:e][mk, 0], y[s:e][mk, 1])[0, 1])
    print('  %4d s  有声比例=%.2f  原相关=%.3f -> 输出相关=%.3f'
          % (s // sr, mk.mean(), c_in, c_out))
