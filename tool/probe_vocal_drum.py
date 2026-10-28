# -*- coding: utf-8 -*-
"""probe_vocal_drum.py —— 人声轨里到底有没有鼓。

为什么前面测不出来
------------------
bench_separate 的 bleed 用的是**前奏段**（纯伴奏），那里 demucs 输出几乎为零，
−62 dB 很好看，但完全不代表"人声和鼓同时响"时的表现。用户报的正是后者。

判据（三个互相独立的物理先验）
------------------------------
1. env_corr   vocals 与 drums 的**逐帧能量包络相关**，只在鼓活跃的帧上算。
             真人声的音量起伏跟着旋律走，不该跟着鼓点走。> 0.5 就是漏了。
2. onset_lift 以 drums 的 onset 为中心，看 vocals 在 onset 前后 ±30 ms 的
             能量跳变。鼓点漏进来时 vocals 会跟着"起跳"。
3. band_ratio vocals 在 40~100 Hz（底鼓基频）与 200~3000 Hz 的比值，
             再减去纯人声参考段的同一比值。底鼓漏进来会让它明显抬高。

用法：
    python tool/probe_vocal_drum.py --start 60 --secs 40
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
DEFAULT_SRC = r"C:/Users/Administrator/Downloads/老狼&王婧_想把我唱给你听.flac"


def load(src, secs, start):
    import soundfile as sf
    i = sf.info(src)
    n = int(secs * i.samplerate) if secs else -1
    x, sr = sf.read(src, start=int(start * i.samplerate), frames=n,
                    dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return x[:, :2].copy(), int(sr)


def four_stems(x, sr, model="htdemucs", device="auto"):
    """直接拿 demucs 的四个源（local_ai.separate 只给 vocals / 其余之和）。"""
    import torch
    import local_ai as la
    from demucs.apply import apply_model

    dev = la._pick_device(device)
    md = la._get_model(model, dev)
    msr = int(getattr(md, "samplerate", 44100))
    w = x
    if sr != msr:
        w = la.resample_to(x, sr, msr)
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
    return {k: v[:n] for k, v in d.items()}, sr, names


def env(x, sr, win=0.01):
    m = x.mean(axis=1) if x.ndim == 2 else x
    n = max(1, int(win * sr))
    k = len(m) // n
    return np.sqrt(np.mean(m[:k * n].reshape(k, n).astype(np.float64) ** 2, axis=1) + 1e-20)


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    if a.std() < 1e-12 or b.std() < 1e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def band_db(x, sr, lo, hi):
    m = x.mean(axis=1)
    X = np.abs(np.fft.rfft(m)) ** 2
    f = np.fft.rfftfreq(len(m), 1.0 / sr)
    return 10 * np.log10(X[(f >= lo) & (f < hi)].sum() + 1e-20)


def analyse(stems, sr, tag=""):
    v = stems["vocals"]
    dr = stems.get("drums", np.zeros_like(v))
    ba = stems.get("bass", np.zeros_like(v))
    ev, ed, eb = env(v, sr), env(dr, sr), env(ba, sr)
    n = min(len(ev), len(ed), len(eb))
    ev, ed, eb = ev[:n], ed[:n], eb[:n]

    # 只取"鼓确实在响"的帧（drums 能量高于它的 60 分位），且人声也在
    drum_on = ed > np.percentile(ed, 60)
    vocal_on = ev > np.percentile(ev, 50)
    m = drum_on & vocal_on
    c_d = corr(ev[m], ed[m]) if m.sum() > 10 else float("nan")
    m2 = vocal_on & (ed < np.percentile(ed, 40))   # 鼓安静但人声在唱
    c_d2 = corr(ev[m2], ed[m2]) if m2.sum() > 10 else float("nan")

    # onset lift：鼓的帧间正跳变处，人声是否跟着跳
    dd = np.diff(ed)
    thr = np.percentile(dd[dd > 0], 90) if (dd > 0).any() else 0
    on = np.where(dd > thr)[0]
    lift = []
    for i in on:
        if 1 <= i < len(ev) - 2:
            before = ev[max(0, i - 3):i].mean()
            after = ev[i:i + 3].mean()
            if before > 1e-9:
                lift.append(20 * np.log10(after / before))
    onset_lift = float(np.median(lift)) if lift else float("nan")

    # 低频比值
    low = band_db(v, sr, 40, 100) - band_db(v, sr, 200, 3000)

    print("  %-10s drums谱 %.1f dB  bass谱 %.1f dB" %
          (tag, band_db(dr, sr, 40, 100) - band_db(dr, sr, 200, 3000),
           band_db(ba, sr, 40, 100) - band_db(ba, sr, 200, 3000)))
    print("  %-10s 包络相关(鼓响时) %6.3f   包络相关(鼓静时) %6.3f" %
          (tag, c_d, c_d2))
    print("  %-10s 鼓 onset 处人声抬升 %5.1f dB   人声低频比 %6.1f dB" %
          (tag, onset_lift, low))
    return dict(env_corr=round(c_d, 3), env_corr_quiet=round(c_d2, 3),
                onset_lift=round(onset_lift, 1), low_ratio=round(low, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--start", type=float, default=60.0)
    ap.add_argument("--secs", type=float, default=40.0)
    ap.add_argument("--model", default="htdemucs")
    ap.add_argument("--device", default="auto")
    a = ap.parse_args()
    x, sr = load(a.src, a.secs, a.start)
    print("%.1f s @ %d Hz（%.1f~%.1f s）\n" % (len(x) / sr, sr, a.start, a.start + len(x) / sr))
    st, sr2, names = four_stems(x, sr, a.model, a.device)
    print("四源:", names)
    analyse(st, sr2, a.model)


if __name__ == "__main__":
    main()
