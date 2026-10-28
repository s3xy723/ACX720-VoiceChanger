# -*- coding: utf-8 -*-
"""probe_drum_suppress.py —— 用鼓源做侧链，把漏进人声的鼓压下去。

原理
----
demucs 已经给了 4 个源，其中 drums 是"鼓在哪、有多响"的可靠估计。
人声轨里的鼓残留，本质是 mask 在"人声与鼓时频重叠"处抉择错误。

于是做一个 Wiener 式时频掩蔽：

    mask = |V|² / (|V|² + α·|D|²)

    |V| 人声幅度   |D| 鼓幅度   α 压制强度

- 人声强、鼓弱的地方 mask≈1 → 人声原样保留
- 人声弱、鼓强的地方 mask≈0 → 鼓被压掉（这正是要的）
- 两者都强 → 按能量比折中（宁可留一点鼓，也不要把人声挖出洞）

两个防 artifacts 的措施：
  1. |D| 先在时频面上做平滑（鼓的谱包络是连续的，平滑不会失真，
     但能避免"逐个频点独立决策"造成的音乐噪声）
  2. mask 的下限 floor 保证人声不会被压成 0（否则会出现"水声"）
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
sys.path.insert(0, os.path.join(ROOT, "tool"))

N_FFT = 2048
HOP = 512
WIN = np.hanning(N_FFT)


def stft(x):
    m = np.asarray(x, dtype=np.float64)
    if m.ndim == 2:
        m = m.mean(axis=1)
    pad = N_FFT // 2
    m = np.concatenate([np.zeros(pad), m, np.zeros(pad)])
    nf = 1 + (len(m) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(nf)[:, None]
    return np.fft.rfft(m[idx] * WIN, axis=1), len(x)


def istft(S, n_out):
    frames = np.fft.irfft(S, n=N_FFT, axis=1) * WIN
    nf = frames.shape[0]
    total = HOP * (nf - 1) + N_FFT
    acc = np.zeros(total)
    wsum = np.zeros(total)
    for i in range(nf):
        acc[i * HOP:i * HOP + N_FFT] += frames[i]
        wsum[i * HOP:i * HOP + N_FFT] += WIN ** 2
    acc /= np.maximum(wsum, 1e-8)
    pad = N_FFT // 2
    y = acc[pad:pad + n_out]
    if len(y) < n_out:
        y = np.concatenate([y, np.zeros(n_out - len(y))])
    return y


def smooth2d(A, kt=3, kf=3):
    """时频面盒式平滑（separably，够用且快）。"""
    def box(a, k, axis):
        if k <= 1:
            return a
        pad = [(0, 0)] * a.ndim
        pad[axis] = (k // 2, k - 1 - k // 2)
        p = np.pad(a, pad, mode="edge")
        c = np.cumsum(p, axis=axis)
        c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
        hi = np.take(c, np.arange(k, c.shape[axis]), axis=axis)
        lo = np.take(c, np.arange(0, c.shape[axis] - k), axis=axis)
        return (hi - lo) / float(k)
    return box(box(A, kt, 0), kf, 1)


def suppress(voc, drums, alpha=0.5, floor=0.25, kt=3, kf=5):
    """返回压掉鼓之后的人声（单声道）。"""
    V, n = stft(voc)
    D, _ = stft(drums)
    m = min(len(V), len(D))
    V, D = V[:m], D[:m]
    mv = np.abs(V)
    md = smooth2d(np.abs(D), kt, kf)
    mask = (mv ** 2) / (mv ** 2 + alpha * md ** 2 + 1e-20)
    mask = np.maximum(mask, floor)
    return istft(V * mask, n), float(np.mean(mask))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=80.0)
    ap.add_argument("--secs", type=float, default=40.0)
    ap.add_argument("--model", default="htdemucs")
    a = ap.parse_args()

    import probe_drum_leak as L
    x, sr = L.load(L.DEFAULT_SRC, a.secs, a.start)
    print("%.0f~%.0f s @ %d Hz，模型 %s\n" % (a.start, a.start + len(x) / sr, sr, a.model))
    st = L.sources(x, sr, a.model, "auto")
    v, d = st["vocals"], st["drums"]
    v_mono = v.mean(axis=1)
    d_mono = d.mean(axis=1)

    base_leak, base_share = L.leak_metrics(v, d, sr)
    print("原始       鼓点处人声残留 %6.1f dB   落在鼓点上的比例 %5.1f dB"
          % (base_leak, base_share))

    V0 = np.abs(L.stft(v))[:, :]
    for alpha in (0.3, 0.6, 1.0, 2.0):
        y, mm = suppress(v_mono, d_mono, alpha=alpha)
        y2 = np.repeat(y.reshape(-1, 1), 2, axis=1).astype(np.float32)
        lk, sh = L.leak_metrics(y2, d, sr)
        # 人声损失：只看"人声强、鼓弱"的时频点，能量掉了多少
        V1 = np.abs(L.stft(y2))
        n = min(len(V0), len(V1))
        d_on = np.abs(L.stft(d))[:n] > np.percentile(np.abs(L.stft(d))[:n], 60)
        keep = 20 * np.log10((V1[:n][~d_on].mean() + 1e-12) / (V0[:n][~d_on].mean() + 1e-12))
        print("alpha=%-4.1f 鼓点处人声残留 %6.1f dB (-%.1f)  落在鼓点上的比例 %5.1f dB   "
              "人声损失 %5.2f dB  mask均值 %.2f"
              % (alpha, lk, base_leak - lk, sh, keep, mm))


if __name__ == "__main__":
    main()
