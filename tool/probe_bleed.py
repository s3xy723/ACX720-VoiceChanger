# -*- coding: utf-8 -*-
"""probe_bleed.py —— 在人声段内测"伴奏残留"，不需要段落标注。

难点
----
前奏段（纯伴奏）测 bleed 很容易，但那恰恰是 demucs 最擅长的场景。
真正听得出"不干净"的是**人声和鼓同时响**的地方，那里无法用"这段没人声"
来切分。所以改用**物理先验**：

  1. lf_corr  人声轨 40~120 Hz 的包络 vs 伴奏轨同频段包络。
              真人声的基频起伏和贝斯/底鼓不同步；相关系数 > 0.7
              说明人声轨里的低频是伴奏漏进来的。
  2. hf_db    人声轨 10~16 kHz 相对 300~3000 Hz 的比值。
              人声在这里只有瞬态齿音；镲片是持续的。偏高 = 镲片漏进来。
  3. onset    人声轨与伴奏轨的短时能量包络**导数**相关。
              鼓点如果同时出现在两轨，导数会同步。

三个都指向同一件事，且互相独立 —— 比单看一个数字可信。
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "tool", "logs", "sep_bench")


def load(tag):
    import soundfile as sf
    d = os.path.join(BENCH, tag)
    v, sr = sf.read(os.path.join(d, "vocals.wav"), dtype="float32", always_2d=True)
    a, _ = sf.read(os.path.join(d, "acc.wav"), dtype="float32", always_2d=True)
    m, _ = sf.read(os.path.join(d, "mix.wav"), dtype="float32", always_2d=True)
    n = min(len(v), len(a), len(m))
    return v[:n], a[:n], m[:n], sr


def _bp(x, sr, lo, hi):
    """FFT 域带通（零相位，够用且不会引入群延迟）。"""
    m = x.mean(axis=1) if x.ndim == 2 else x
    X = np.fft.rfft(m)
    f = np.fft.rfftfreq(len(m), 1.0 / sr)
    X[(f < lo) | (f > hi)] = 0.0
    return np.fft.irfft(X, len(m))


def _env(x, sr, win=0.02):
    n = max(1, int(round(win * sr)))
    k = len(x) // n
    if k < 2:
        return np.zeros(1)
    r = np.sqrt(np.mean(x[:k * n].reshape(k, n).astype(np.float64) ** 2, axis=1) + 1e-20)
    return r


def _corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    a = a - a.mean()
    b = b - b.mean()
    if a.std() < 1e-12 or b.std() < 1e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def analyse(tag):
    v, a, m, sr = load(tag)
    # ---- 只看"人声确实存在"的帧，排除静音段干扰 ----
    ev = _env(v.mean(axis=1), sr)
    thr = np.percentile(ev, 60)          # 人声能量前 40% 的帧
    live = ev >= thr

    out = {"tag": tag, "secs": round(len(v) / sr, 1)}

    # 1) 低频包络相关
    vl = _env(_bp(v, sr, 40, 120), sr)
    al = _env(_bp(a, sr, 40, 120), sr)
    n = min(len(vl), len(al), len(live))
    out["lf_corr"] = _corr(vl[:n][live[:n]], al[:n][live[:n]])

    # 2) 高频占比（全段，因为齿音本来就是瞬态）
    def band_energy(x, lo, hi):
        m1 = x.mean(axis=1)
        X = np.abs(np.fft.rfft(m1)) ** 2
        f = np.fft.rfftfreq(len(m1), 1.0 / sr)
        return X[(f >= lo) & (f < hi)].sum() + 1e-20
    hv = band_energy(v, 10000, 16000)
    mv_ = band_energy(v, 300, 3000)
    ha = band_energy(a, 10000, 16000)
    out["hf_db"] = float(10 * np.log10(hv / mv_))
    out["hf_vs_acc_db"] = float(10 * np.log10((hv / mv_) / (ha / band_energy(a, 300, 3000))))

    # 3) onset 相关（包络的帧间差分）
    dv = np.diff(_env(v.mean(axis=1), sr, 0.01))
    da = np.diff(_env(a.mean(axis=1), sr, 0.01))
    k = min(len(dv), len(da))
    out["onset_corr"] = _corr(np.maximum(dv[:k], 0), np.maximum(da[:k], 0))

    # 4) 参考：混音自身的 lf 相关（下限，说明这歌本身贝斯有多居中）
    ml = _env(_bp(m, sr, 40, 120), sr)
    out["mix_lf_self"] = float("nan")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", nargs="+", required=True)
    a = ap.parse_args()
    hdr = "%-18s %6s %9s %9s %11s %9s" % ("tag", "secs", "lf_corr",
                                          "hf_db", "hf_vs_acc", "onset_c")
    print(hdr)
    print("-" * len(hdr))
    for t in a.tags:
        r = analyse(t)
        print("%-18s %6.1f %9.3f %9.1f %11.1f %9.3f" %
              (r["tag"], r["secs"], r["lf_corr"], r["hf_db"],
               r["hf_vs_acc_db"], r["onset_corr"]))
    print()
    print("判读：")
    print("  lf_corr    < 0.3 好，> 0.6 说明人声轨里的低频来自伴奏（会听到贝斯/底鼓）")
    print("  hf_db      人声轨 10-16k 相对 300-3k：正常 -35 dB 以下；偏高 = 镲片漏进来")
    print("  hf_vs_acc  人声轨的高频占比 相对 伴奏轨：< 0 dB 好，> 0 dB = 人声轨比伴奏轨还亮")
    print("  onset_c    鼓点同步度：< 0.2 好")


if __name__ == "__main__":
    main()
