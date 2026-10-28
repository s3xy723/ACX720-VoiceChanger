# -*- coding: utf-8 -*-
"""
exp_demucs_vocal.py —— 找出"什么样的激励才会被 demucs 判成人声"

背景：verify_local_ai 里合成的"人声"是一个稳态谐波堆（固定共振峰、
无颤音、无气声、无辅音、音节之间不停顿）。demucs 把它判给了 other
源（SI-SDR +3.65 dB），vocals 源几乎是空的（rms 0.001 vs 0.092）。

这不是 demucs 的问题 —— 一个纯稳态谐波堆在听感上就是合成器 pad，
模型没理由把它当人声。要客观验收"神经网络能挖人声"，激励首先得
**是人声**。

这里逐项叠加真实歌唱的特征，看每一项分别起多大作用：
    颤音 / 气声 / 辅音爆破 / 句间停顿 / 起音滑音 / 相对音量
判据：vocals 源相对真人声的 SI-SDR。
"""

import os
import sys
import math

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.normpath(os.path.join(HERE, "..", "host"))
sys.path.insert(0, HOST)
sys.path.insert(0, HERE)

import local_ai as LA                              # noqa: E402
from verify_local_ai import si_sdr                 # noqa: E402

SR = 48000
MSR = 44100
DUR = 6.0

NAMES = ["drums", "bass", "other", "vocals"]

_model = None


def get_model():
    global _model
    if _model is None:
        from demucs.pretrained import get_model as _g
        m = _g("htdemucs")
        m.eval()
        m.to("cuda")
        _model = m
    return _model


def stems(mix, sr):
    m = get_model()
    work = LA.resample_to(mix, sr, MSR).astype(np.float32)
    w = torch.from_numpy(np.ascontiguousarray(work.T)).float()
    ref = w.mean(0)
    std = ref.std() + 1e-8
    wn = ((w - ref.mean()) / std)[None]
    with torch.no_grad():
        from demucs.apply import apply_model
        out = apply_model(m, wn, shifts=0, split=True, overlap=0.25,
                          progress=False, device="cuda", segment=None)[0]
    out = out.float().cpu().numpy() * float(std) + float(ref.mean())
    return [out[i].T for i in range(out.shape[0])]


# ---------------------------------------------------------------- 人声合成

NOTES = [(220.0, (700.0, 1200.0, 2500.0)),
         (247.0, (620.0, 1100.0, 2600.0)),
         (262.0, (560.0, 1000.0, 2700.0)),
         (294.0, (520.0, 1450.0, 2750.0)),
         (220.0, (700.0, 1200.0, 2500.0)),
         (196.0, (760.0, 1150.0, 2450.0))]


def synth_vocal(n, sr, vib=0.0, breath=0.0, cons=0.0, glide=0.0,
                gap=0.0, syl=4, seed=11):
    """逐项可开关的人声合成器。

    vib    颤音深度（F0 的 ±3%×vib，5.5 Hz）
    breath 气声（带通噪声）相对振幅
    cons   辅音爆破（音节头的短噪声）相对振幅
    glide  起音滑音 + 元音内共振峰滑动
    gap    句间停顿比例（每个音符尾部留空的时长比）
    """
    rng = np.random.RandomState(seed)
    y = np.zeros(n)
    seg = n // len(NOTES)

    # 全局气声底噪（3~8 kHz 一带）
    if breath > 0:
        nb = rng.randn(n)
        # 简单带通：两次一阶差分 + 移动平均
        nb = np.convolve(nb, np.hanning(9) / np.hanning(9).sum(), "same")
        nb = np.diff(np.concatenate([[0.0], nb]))
        nb = np.diff(np.concatenate([[0.0], nb]))
        nb = nb / (np.std(nb) + 1e-9)

    for i, (f0, form) in enumerate(NOTES):
        b = i * seg
        m = seg if i < len(NOTES) - 1 else n - seg * (len(NOTES) - 1)
        t = np.arange(m) / float(sr)

        # --- 基频轮廓：起音滑音 + 颤音 ---
        f = np.full(m, f0)
        if glide:
            # 起音 60 ms 内从上一个音高滑过来
            k = int(0.06 * sr)
            if k > 1 and i > 0:
                f[:k] = NOTES[i - 1][0] + (f0 - NOTES[i - 1][0]) * np.linspace(0, 1, k)
            f = f * (1.0 + 0.01 * glide * np.sin(2 * np.pi * 0.9 * t))
        if vib:
            f = f * (1.0 + 0.03 * vib * np.sin(2 * np.pi * 5.5 * t
                                               + rng.uniform(0, 6.28)))
        ph = 2.0 * np.pi * np.cumsum(f) / float(sr)

        hs = np.arange(1, int(0.92 * (sr / 2.0) / f0) + 1)
        a = np.zeros(hs.size)
        for j, (ff, amp) in enumerate(zip(form, (1.0, 0.7, 0.32))):
            bw = (100.0, 140.0, 200.0)[j]
            a += amp / (1.0 + ((hs * f0 - ff) / (0.5 * bw)) ** 2)
        hph = rng.uniform(0.0, 2.0 * math.pi, hs.size)

        s = np.zeros(m)
        for k2 in range(hs.size):
            s += a[k2] * np.sin(hs[k2] * ph + hph[k2])

        # --- 音节包络 ---
        env = np.ones(m)
        syl_len = m // syl
        for q in range(syl):
            bq, eq = q * syl_len, min(m, (q + 1) * syl_len)
            wlen = max(2, eq - bq)
            w = np.hanning(wlen * 2)[:wlen]
            env[bq:eq] = 0.10 + 0.90 * w
        # --- 句间停顿 ---
        if gap:
            off = int(m * (1.0 - gap))
            env[off:] = 0.0
        s = s * env

        # --- 气声（跟着音节包络走）---
        if breath:
            s = s + breath * 0.25 * nb[b:b + m] * env * (np.std(s) + 1e-9)

        # --- 辅音爆破：每个音节头的 12 ms 噪声 ---
        if cons:
            amp = cons * 0.5 * (np.std(s[np.abs(s) > 0]) + 1e-9)
            for q in range(syl):
                bq = q * syl_len
                kc = int(0.012 * sr)
                if bq + kc <= m:
                    burst = rng.randn(kc) * np.hanning(kc)
                    # 频谱倾斜：擦音偏高频
                    burst = np.diff(np.concatenate([[0.0], burst]))
                    s[bq:bq + kc] += amp * burst / (np.std(burst) + 1e-9) * 0.6

        s = s / (np.max(np.abs(s)) + 1e-12)
        y[b:b + m] = s
    return y


def make_mix(voc_mono, vocal_db=0.0, seed=3):
    """把人声压成立体声居中，加上居中的贝斯/底鼓和左右有差异的伴奏。"""
    n = voc_mono.size
    rng = np.random.RandomState(seed)
    t = np.arange(n) / float(SR)

    k = 10.0 ** (vocal_db / 20.0)
    voc = voc_mono * k

    bass = (0.45 * np.sin(2 * math.pi * 82.0 * t)
            + 0.20 * np.sin(2 * math.pi * 164.0 * t))
    bass = bass * (0.5 + 0.5 * np.sin(2 * math.pi * 1.6 * t) ** 2)

    kick = np.zeros(n)
    for q in range(int(n / (0.5 * SR))):
        b = int(q * 0.5 * SR)
        mm = int(0.12 * SR)
        if b + mm > n:
            break
        u = np.arange(mm) / float(SR)
        f = 55.0 + 90.0 * np.exp(-u / 0.02)
        kick[b:b + mm] = 0.75 * np.exp(-u / 0.045) * np.sin(2 * math.pi * f * u)

    hat = np.zeros(n)
    for q in range(int(n / (0.25 * SR))):
        b = int(q * 0.25 * SR)
        mm = int(0.03 * SR)
        if b + mm > n:
            break
        hat[b:b + mm] = rng.uniform(-1, 1, mm) * np.exp(-np.arange(mm) / (0.006 * SR))
    hat = hat * 0.18

    pad_l = 0.10 * np.sin(2 * math.pi * 1180.0 * t) + 0.07 * np.sin(2 * math.pi * 1810.0 * t)
    pad_r = 0.10 * np.sin(2 * math.pi * 1180.0 * t + 1.1) + 0.07 * np.sin(2 * math.pi * 1830.0 * t)

    acc = np.empty((n, 2))
    acc[:, 0] = bass + kick + hat + pad_l
    acc[:, 1] = bass + kick + rng.uniform(-1, 1, n) * 0.18 + pad_r

    mix = np.empty((n, 2))
    mix[:, 0] = voc + acc[:, 0]
    mix[:, 1] = voc + acc[:, 1]
    mx = np.max(np.abs(mix))
    if mx > 0.92:
        mix, voc, acc = mix * (0.92 / mx), voc * (0.92 / mx), acc * (0.92 / mx)
    vt = np.empty((n, 2))
    vt[:, 0] = voc
    vt[:, 1] = voc
    return mix.astype(np.float32), vt.astype(np.float32), acc.astype(np.float32)


def main():
    n = int(SR * DUR)
    print("=" * 74)
    print("什么激励才会被 demucs 判成人声？  （判据：vocals 源 vs 真人声 SI-SDR）")
    print("=" * 74)
    print("%-40s %10s %10s %s" % ("变体", "vocals", "other", "最优源"))

    variants = [
        ("基线：稳态谐波堆", dict(), 0.0),
        ("+ 颤音", dict(vib=1.0), 0.0),
        ("+ 颤音 + 气声", dict(vib=1.0, breath=0.35), 0.0),
        ("+ 颤音 + 气声 + 辅音", dict(vib=1.0, breath=0.35, cons=1.0), 0.0),
        ("+ 上述 + 滑音", dict(vib=1.0, breath=0.35, cons=1.0, glide=1.0), 0.0),
        ("+ 上述 + 句间停顿", dict(vib=1.0, breath=0.35, cons=1.0, glide=1.0,
                                gap=0.15), 0.0),
        ("全套 + 人声抬高 6 dB", dict(vib=1.0, breath=0.35, cons=1.0, glide=1.0,
                                  gap=0.15), 6.0),
    ]

    best = None
    for name, kw, gdb in variants:
        voc = synth_vocal(n, SR, **kw)
        mix, vt, at = make_mix(voc, vocal_db=gdb)
        st = stems(mix, SR)
        vtm = LA.resample_to(vt, SR, MSR)
        sc = [si_sdr(s, vtm) for s in st]
        j = int(np.argmax(sc))
        print("%-40s %+9.2f %+9.2f %s"
              % (name, sc[3], sc[2], NAMES[j]))
        if best is None or sc[3] > best[1]:
            best = (name, sc[3], kw, gdb)

    print()
    print("最优：%s   vocals SI-SDR = %+.2f dB" % (best[0], best[1]))
    print("参数：%s  人声增益 %+.1f dB" % (best[2], best[3]))


if __name__ == "__main__":
    main()
