# -*- coding: utf-8 -*-
"""向量化的基频估计对比：现有自相关 vs 次谐波累加 vs 频域谐波累加(HPS)。"""
import sys, time
import numpy as np
sys.path.insert(0, r'C:\Users\s3xy\WorkBuddy\2026-10-10-21-21-17\acx720_voice_changer\host')
import audio_io

P = r'C:\Users\s3xy\Downloads\老狼&王婧_想把我唱给你听.flac'
a, sr = audio_io.load_audio(P, {})
x = a.astype(np.float64)
mid = 0.5 * (x[:, 0] + x[:, 1])
N = len(mid)

NFFT, HOP, F0_WIN = 1024, 256, 2048
W = np.hanning(NFFT + 1)[:NFFT]
WIN2 = np.hanning(F0_WIN + 1)[:F0_WIN]
nfr = (N - NFFT) // HOP

t0 = time.time()
# ---- 人声估计：只用中置掩蔽（不接谐波梳，避免自反馈）----
idx = np.arange(nfr)[:, None] * HOP + np.arange(NFFT)[None, :]
SGL = mid[idx] * W                                  # (nfr, NFFT)
SGL_L = x[idx, 0] * W
SGL_R = x[idx, 1] * W
FL = np.fft.rfft(SGL)                               # 保留相位，别用 |X| 反变换
MAGM = np.abs(FL)
MAGS = np.abs(np.fft.rfft(0.5 * (SGL_L - SGL_R)))
CMASK = np.clip((MAGM - MAGS) / (MAGM + 1e-12), 0, 1)
MAG = MAGM * CMASK
v = np.fft.irfft(FL * CMASK, NFFT)                  # (nfr, NFFT)
# 重叠相加
vtime = np.zeros(N)
cnt = np.zeros(N)
for k in range(nfr):
    vtime[k * HOP:k * HOP + NFFT] += v[k] * W
    cnt[k * HOP:k * HOP + NFFT] += W * W
vtime /= np.maximum(cnt, 1e-9)
print('人声估计用时 %.1f s' % (time.time() - t0))

# ---- 取 F0 分析窗 ----
pos = np.arange(nfr) * HOP + HOP - F0_WIN
ok = pos >= 0
pos_c = np.clip(pos, 0, N - F0_WIN)
FR = vtime[pos_c[:, None] + np.arange(F0_WIN)[None, :]] * WIN2     # (nfr, F0_WIN)
FR = FR - FR.mean(axis=1, keepdims=True)
nff = 4096
SPEC = np.abs(np.fft.rfft(FR, nff, axis=1))                        # (nfr, nff/2+1)
freq = np.fft.rfftfreq(nff, 1.0 / sr)
FMIN, FMAX = 70.0, 500.0

# ---------- A) 现有：自相关取第一个显著局部极大 ----------
def est_first_peak():
    f = np.fft.rfftfreq(nff, 1.0 / sr)
    S = np.fft.rfft(FR, nff, axis=1)
    S[:, (f < FMIN * 0.6) | (f > 1200)] = 0
    acf = np.fft.irfft(S * np.conj(S), nff, axis=1)[:, :F0_WIN]
    acf = acf / (np.abs(acf[:, :1]) + 1e-12)
    lo = max(2, int(sr / (FMAX * 1.3))); hi = min(F0_WIN - 1, int(sr / (FMIN * 0.9)))
    r = acf[:, lo:hi + 1]
    g = r.max(axis=1, keepdims=True)
    d = np.diff(r, axis=1)
    peak = (d[:, :-1] > 0) & (d[:, 1:] <= 0)
    cand = np.where(peak & (r[:, 1:-1] > 0.62 * g), r[:, 1:-1], -1.0)
    first = np.argmax(cand > 0, axis=1)
    has = cand.max(axis=1) > 0
    best = r.argmax(axis=1)
    i = np.where(has, first + 1, best)
    return sr / (lo + i), r[np.arange(len(i)), i]

# ---------- B) 自相关 + 次谐波累加 ----------
K = 5
def est_subharm():
    f = np.fft.rfftfreq(nff, 1.0 / sr)
    S = np.fft.rfft(FR, nff, axis=1)
    S[:, (f < FMIN * 0.55) | (f > 2500)] = 0
    acf = np.fft.irfft(S * np.conj(S), nff, axis=1)[:, :F0_WIN]
    acf = acf / (np.abs(acf[:, :1]) + 1e-12)
    lo = max(2, int(sr / (FMAX * 1.15))); hi = min(F0_WIN - 1, int(sr / (FMIN * 0.9)))
    lags = np.arange(lo, hi + 1)
    kk = np.arange(1, K + 1)[:, None]
    mx = kk * lags[None, :]
    valid = (mx < F0_WIN).astype(np.float64)
    mx_safe = np.clip(mx, 0, F0_WIN - 1)
    wgt = valid / kk                                   # (K, nlags)
    sub = acf[:, mx_safe]                              # (nfr, K, nlags)
    score = (sub * wgt[None, :, :]).sum(axis=1)
    bi = score.argmax(axis=1)
    return sr / lags[bi], score[np.arange(len(bi)), bi]

# ---------- C) 频域谐波累加 HPS（在帧子集上评估，向量化） ----------
H = 12
def est_hps():
    cand = np.geomspace(FMIN, FMAX, 160)
    hs = np.arange(1, H + 1)[:, None]
    fc = hs * cand[None, :]
    bw = np.maximum(2.0 * (sr / nff), 0.030 * fc)
    i0 = np.searchsorted(freq, fc - bw).clip(0, len(freq) - 1)
    i1 = np.searchsorted(freq, fc + bw).clip(1, len(freq))
    wgt = 1.0 / hs
    # 预先展开成 (C*H, 2) 的区间表，逐候选算（候选只有 160 个，向量化在帧维）
    sc = np.full(len(FR), -1.0)
    val = np.zeros(len(FR))
    for c in range(len(cand)):
        a0 = i0[:, c]; a1 = i1[:, c]
        acc = np.zeros(len(FR))
        for h in range(H):
            if fc[h, c] > sr * 0.45:
                break
            if a1[h] > a0[h]:
                acc += SPEC[:, a0[h]:a1[h]].max(axis=1) * wgt[h, 0]
        upd = acc > val
        val = np.where(upd, acc, val)
        sc = np.where(upd, cand[c], sc)
    return sc, val


sub = np.arange(0, nfr, 12)          # 先每 12 帧取一帧做对比
print('对比用 %d 帧（每 12 帧取 1）' % len(sub))
FR_ALL, SPEC_ALL = FR, SPEC
FR, SPEC = FR_ALL[sub], SPEC_ALL[sub]
ok_sub = ok[sub]

res = {}
for name, fn in (('现有:自相关首个峰', est_first_peak),
                 ('自相关+次谐波累加', est_subharm),
                 ('频域谐波累加 HPS', est_hps)):
    t0 = time.time(); f0, c = fn(); dt = time.time() - t0
    good = ok_sub & (c > 0) & (f0 > 60) & (f0 < 600)
    vv = f0[good]
    h, e = np.histogram(vv, bins=np.arange(60, 601, 20))
    top = np.argsort(h)[-6:][::-1]
    print('\n== %s ==  %.2f s（%d 帧）  有效 %d 帧  中位 %.0f Hz'
          % (name, dt, len(sub), good.sum(), np.median(vv) if len(vv) else 0))
    print('   主要音域: ' + '  '.join('%d-%d:%d' % (e[i], e[i+1], h[i]) for i in top))
    res[name] = f0
np.save('tool/_f0_tmp.npy', np.stack([res[k] for k in res]))
print('\n已存 tool/_f0_tmp.npy')
