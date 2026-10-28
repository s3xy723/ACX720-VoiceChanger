# -*- coding: utf-8 -*-
"""看 HPS 在干净人声上到底给了哪些候选最高分。"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'host')))
import dsp_core as dsp
from vocal_split import NFFT, HOP, _WIN

voc = np.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_demucs_voc.npy'))
sr = 44100
v = 0.5 * (voc[:, 0] + voc[:, 1])
print('demucs 人声 %.1f s' % (len(v) / sr))

sp = dsp.VocalSplitter(sr)
sp.configure()
# 男声独唱在 0-16 s，女声独唱在 16-24 s（这段是 8-32 s 的切片）
for tag, t0, t1 in (('男声独唱', 2.0, 6.0), ('女声独唱', 17.0, 21.0)):
    print('\n===== %s =====' % tag)
    for tt in np.arange(t0, t1, 1.0):
        k = int(tt * sr / HOP)
        seg = v[k * HOP:k * HOP + NFFT]
        if len(seg) < NFFT:
            continue
        F = np.fft.rfft(seg * _WIN)
        mag = np.abs(F)
        sal = sp._salience(mag)
        top = np.argsort(sal)[-8:][::-1]
        print('  t=%4.1fs  前 8 名: %s' % (
            tt, '  '.join('%.0fHz(%.1f)' % (sp._cands[i], sal[i]) for i in top)))
        # 直接看谱峰在哪儿
        pk = np.where((mag[1:-1] > mag[:-2]) & (mag[1:-1] > mag[2:]))[0] + 1
        pk = pk[np.argsort(mag[pk])[-6:]]
        f = np.fft.rfftfreq(NFFT, 1.0 / sr)
        print('        最强谱峰: %s' % ' '.join('%.0f' % f[i] for i in sorted(pk)))

# 全局：把整段人声的"第一谐波"位置统计一下
print('\n===== 全局频谱峰分布 =====')
nf = (len(v) - NFFT) // HOP
acc = np.zeros(NFFT // 2 + 1)
for k in range(0, nf, 8):
    seg = v[k * HOP:k * HOP + NFFT]
    acc += np.abs(np.fft.rfft(seg * _WIN))
acc /= acc.max()
f = np.fft.rfftfreq(NFFT, 1.0 / sr)
b = (f > 60) & (f < 700)
top = np.argsort(acc * b)[-14:]
print('  平均谱最强频点:', '  '.join('%.0f(%.2f)' % (f[i], acc[i]) for i in sorted(top)))
