# -*- coding: utf-8 -*-
"""probe_drum_leak.py —— 精确量"人声轨里带鼓"。

为什么之前的指标不够
--------------------
包络相关只能抓"整段一起起伏"。真正的泄漏往往只在**鼓点那一瞬间、
某个频带**出现，包络一平均就看不出来了。

本脚本用时频点级别的判据：

    drums 强 且 vocals 弱 的时频点（鼓在响、这里不该有人声）
        ↓
    在这些点上测 vocals 轨的幅度 相对 drums 的幅度
        ↓
    leak_db = 20log10(mean|V| / mean|D|)   ← 这一项直接就是"鼓漏进人声多少"

另外给一个更有听感意义的数：
    drum_share = 人声轨总能量中，落在"鼓活跃时频点"上的比例。
    真人声会落在自己的谐波上，和鼓的时频点重叠不多；
    漏鼓会让这个比例明显上升。

用法：
    python tool/probe_drum_leak.py --start 80 --secs 40 --models htdemucs mdx_extra
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
DEFAULT_SRC = r"C:/Users/Administrator/Downloads/老狼&王婧_想把我唱给你听.flac"

N_FFT = 2048
HOP = 512


def load(src, secs, start):
    import soundfile as sf
    i = sf.info(src)
    n = int(secs * i.samplerate) if secs else -1
    x, sr = sf.read(src, start=int(start * i.samplerate), frames=n,
                    dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return x[:, :2].copy(), int(sr)


def stft(x):
    """(n,2) -> 复数谱 (frames, bins)，取声道平均（人声/鼓都是居中的）。"""
    m = x.mean(axis=1).astype(np.float64)
    win = np.hanning(N_FFT)
    pad = N_FFT // 2
    m = np.concatenate([np.zeros(pad), m, np.zeros(pad)])
    nf = 1 + max(0, (len(m) - N_FFT) // HOP)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(nf)[:, None]
    return np.fft.rfft(m[idx] * win, axis=1)


def sources(x, sr, model, device="auto"):
    """拿 4 源。返回 {名字: (n,2)}。"""
    import torch
    import local_ai as la
    from demucs.apply import apply_model
    dev = la._pick_device(device)
    md = la._get_model(model, dev)
    msr = int(getattr(md, "samplerate", 44100))
    w = la.resample_to(x, sr, msr) if sr != msr else x
    t = torch.from_numpy(np.ascontiguousarray(w.T)).float()
    ref = t.mean(0)
    std = ref.std() + 1e-8
    wn = (t - ref.mean()) / std
    seg = la._model_segment(md) or 10.0
    with torch.no_grad():
        out = apply_model(md, wn[None], shifts=0, split=True, overlap=0.5,
                          progress=False, device=dev, segment=seg)[0]
    out = out.float().cpu().numpy() * float(std) + float(ref.mean())
    names = list(getattr(md, "sources", ["drums", "bass", "other", "vocals"]))
    d = {n: out[i].T.astype(np.float32) for i, n in enumerate(names)}
    if sr != msr:
        d = {k: la.resample_to(v, msr, sr) for k, v in d.items()}
    n = min(len(v) for v in d.values())
    return {k: v[:n] for k, v in d.items()}


def leak_metrics(voc, drums, sr):
    V = np.abs(stft(voc))
    D = np.abs(stft(drums))
    n = min(len(V), len(D))
    V, D = V[:n], D[:n]
    # 只看 60 Hz ~ 12 kHz（鼓的基频到镲片）
    f = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    band = (f >= 60) & (f <= 12000)
    V, D = V[:, band], D[:, band]

    d_on = D > np.percentile(D, 92)          # 鼓最响的 8% 时频点
    v_off = V < np.percentile(V, 60)         # 人声在该点很弱
    m = d_on & v_off
    if m.sum() < 100:
        return float("nan"), float("nan")
    leak = 20 * np.log10((V[m].mean() + 1e-12) / (D[m].mean() + 1e-12))

    # 人声轨能量里落在"鼓点"上的比例（去掉整体能量尺度的影响）
    share = 20 * np.log10((V[d_on].mean() + 1e-12) / (V[~d_on].mean() + 1e-12))
    return float(leak), float(share)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--start", type=float, default=80.0)
    ap.add_argument("--secs", type=float, default=40.0)
    ap.add_argument("--models", nargs="+", default=["htdemucs", "mdx_extra"])
    ap.add_argument("--device", default="auto")
    a = ap.parse_args()
    x, sr = load(a.src, a.secs, a.start)
    print("%.0f~%.0f s @ %d Hz\n" % (a.start, a.start + len(x) / sr, sr))

    got = {}
    for m in a.models:
        got[m] = sources(x, sr, m, a.device)
        lk, sh = leak_metrics(got[m]["vocals"], got[m]["drums"], sr)
        print("%-12s 鼓点处人声残留 %6.1f dB   人声轨能量落在鼓点上的比例 %6.1f dB"
              % (m, lk, sh))

    if len(got) >= 2:
        ks = list(got)
        n = min(len(got[k]["vocals"]) for k in ks)
        for w in (0.5, 0.4, 0.6):
            v = w * got[ks[0]]["vocals"][:n] + (1 - w) * got[ks[1]]["vocals"][:n]
            d0 = got[ks[0]]["drums"][:n]
            lk, sh = leak_metrics(v, d0, sr)
            print("%-12s 鼓点处人声残留 %6.1f dB   人声轨能量落在鼓点上的比例 %6.1f dB"
                  % ("ens w=%.1f" % w, lk, sh))


if __name__ == "__main__":
    main()
