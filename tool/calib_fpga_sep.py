#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""calib_fpga_sep.py —— 用 AI 分离结果精校 FPGA 的人声/伴奏分离算法

背景
----
FPGA 上做的不是神经网络，而是一套**经典 DSP**（见 FPGA_SEPARATION_DESIGN.md §1）：

    mid=(L+R)/2, side=(L-R)/2  →  加窗 FFT
      →  逐频点中置度 c=(|mid|-|side|)/|mid|
      →  V = c·mid                      （人声估计）
      →  |V|² 的 IFFT 取自相关 → 男女两个音域各找一条基频
      →  两把谐波梳竞争掩蔽 → Vm / Vf
      →  IFFT + 重叠相加
      →  左 = 人声，右 = 伴奏

这套算法在 host 侧有一份逐点等价的 Python 实现（`host/vocal_split.py`），
本脚本就是拿它当"FPGA 代理"，用**真实歌曲 + AI 分离结果**当基准，
逐个参数试，看哪一组最接近 AI。

为什么不能用合成素材
--------------------
见 三阶段验收报告 §2.5：demucs 会把合成音判进 other 轨（vocals 能量占比
0.0%），合成素材上的 SI-SDR 是噪声比噪声。必须用真人唱歌。

指标
----
  SI-SDR  尺度无关信噪比，越小越像"AI 的那一轨"
  SIR     把估计投影到 [AI人声, AI伴奏] 张成的子空间，
          看目标能量与干扰能量的比值 —— 这才是"分离度"的本质
  捕获率   AI 人声能量里有多大比例被拿到了（-dB 表示漏掉多少）

用法
----
    # 先跑一次参考（只需一次）
    python tool/calib_make_ref.py

    # 基线
    python tool/calib_fpga_sep.py

    # 参数扫描（在 60 s 片段上扫，快）
    python tool/calib_fpga_sep.py --sweep --clip 40:100

    # 用扫出来的最优参数在整首上复核
    python tool/calib_fpga_sep.py --best
"""
from __future__ import print_function

import argparse
import itertools
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOP, "host"))

CAL = os.path.join(TOP, "work", "calib")


# ------------------------------------------------------------------ 指标

def si_sdr(est, ref, eps=1e-20):
    """尺度无关信噪比（dB）。est ≈ α·ref 时最理想。"""
    e = np.asarray(est, dtype=np.float64).ravel()
    r = np.asarray(ref, dtype=np.float64).ravel()
    n = min(e.size, r.size)
    e, r = e[:n], r[:n]
    den = float(np.dot(r, r)) + eps
    if den <= eps:
        return float("nan")
    a = float(np.dot(e, r)) / den
    tgt = a * r
    err = e - tgt
    num = float(np.dot(tgt, tgt))
    den2 = float(np.dot(err, err)) + eps
    if num <= 0:
        return float("nan")
    return 10.0 * np.log10(num / den2)


def project_sir(est, ref_t, ref_i, eps=1e-20):
    """把 est 最小二乘投影到 [ref_t, ref_i] 上，返回 (SIR_dB, 捕获率_dB)。

    比 SI-SDR 更贴"分离度"的直觉：干扰源在估计里占多少。
    """
    e = np.asarray(est, dtype=np.float64).ravel()
    t = np.asarray(ref_t, dtype=np.float64).ravel()
    i = np.asarray(ref_i, dtype=np.float64).ravel()
    n = min(e.size, t.size, i.size)
    e, t, i = e[:n], t[:n], i[:n]
    A = np.stack([t, i], axis=1)
    try:
        c, *_ = np.linalg.lstsq(A, e, rcond=None)
    except np.linalg.LinAlgError:
        return float("nan"), float("nan")
    et = float(np.sum((c[0] * t) ** 2))
    ei = float(np.sum((c[1] * i) ** 2))
    sir = 10.0 * np.log10((et + eps) / (ei + eps))
    cap = 10.0 * np.log10((et + eps) / (float(np.sum(t * t)) + eps))
    return sir, cap


# ------------------------------------------------------------------ 载入

def parse_secs(txt):
    """'40:100' -> (40.0, 100.0)；'' -> None"""
    if not txt:
        return None
    a, _, b = txt.partition(":")
    return (float(a) if a else 0.0, float(b) if b else None)


def load_refs(clip="", sr_target=None, secs=None):
    """读参考；sr_target 与文件不同时用带限 sinc 重采样。

    ★ 为什么要重采样：FPGA / UART 通路的实际帧率是 **24414.0625 Hz**
      （50 MHz / 2048），而歌曲是 44.1 kHz。NFFT=1024 时两者的 bin 间距
      是 23.8 Hz vs 43.0 Hz —— 谐波梳的齿宽（`max(35 Hz, 3%)`）、
      幅度门限、5 kHz 以上的齿音过渡，全都与 bin 间距相关。在 44.1 kHz
      上调出来的参数搬到 24.414 kHz 会明显偏。
    """
    import soundfile as sf
    tag = "clip" if clip else "full"
    paths = {k: os.path.join(CAL, "ref_%s_%s.wav" % (tag, k))
             for k in ("mix", "vocal", "acc")}
    for k, p in paths.items():
        if not os.path.isfile(p):
            raise SystemExit(
                "缺少参考文件：%s\n请先跑：python tool/calib_make_ref.py%s"
                % (p, (" --clip " + clip) if clip else ""))
    out = {}
    sr = None
    for k, p in paths.items():
        x, s = sf.read(p, dtype="float64", always_2d=True)
        out[k] = x
        sr = s
    # ★ 先切片再重采样：整首 289 s 做带限 sinc 要几十秒，扫参数时没必要
    if secs:
        i0 = int(secs[0] * sr)
        i1 = int(secs[1] * sr) if secs[1] else len(out["mix"])
        for k in list(out):
            out[k] = out[k][i0:i1]

    if sr_target and int(sr_target) != int(sr):
        import local_ai
        print("重采样 %d -> %d Hz（带限 sinc）…" % (sr, int(sr_target)),
              flush=True)
        for k in list(out):
            out[k] = local_ai.resample_to(out[k], int(sr), int(sr_target))
        sr = int(sr_target)
    return out["mix"], out["vocal"], out["acc"], sr


# ------------------------------------------------------------------ 主评估

# 2026-10-21 改版前的原始默认值（对照用，别跟着改）
BASE_OLD = dict(depth=1.0, floor=0.06, strength=1.0, broadband=0.20,
                harmonic=True, f0_hz=0.0, f0_min=70.0, f0_max=500.0,
                swap=False, xfade_ms=30.0, hang_ms=150.0, conf_min=0.35,
                gate_db=-40.0, bass_protect_hz=120.0,
                comb_bw_min=35.0, comb_bw_rel=0.03, comb_fmax=11000.0,
                comb_harm_max=48, f0_whiten_hz=0.0,
                sub_widen=1.0, sub_broadband=0.20, sub_dilate=0)

# 现在的默认值（= vocal_split.configure 的默认值）
BASE = dict(BASE_OLD)
BASE.update(comb_bw_min=18.0, comb_bw_rel=0.01, f0_whiten_hz=250.0,
            sub_widen=2.5, sub_dilate=2)

# 逐参数扫描的候选值。★ comb_bw_* 是这次精校才发现的大杠杆：
# 齿宽原来硬编码 35 Hz / 3%，在 fs=24.4 kHz、人声谐波间隔只有 150~400 Hz
# 的情况下齿几乎连成一片，等于没有选择性。
GRIDS = [
    ("comb_bw_rel", [0.030, 0.020, 0.014, 0.010, 0.007, 0.005]),
    ("comb_bw_min", [35.0, 26.0, 18.0, 13.0, 9.0]),
    ("broadband", [0.0, 0.05, 0.10, 0.20, 0.30, 0.45]),
    ("gate_db", [-60.0, -50.0, -40.0, -32.0, -26.0, -20.0]),
    ("strength", [0.8, 1.0, 1.3, 1.6, 2.0, 2.6]),
    ("floor", [0.0, 0.03, 0.06, 0.12, 0.20]),
    ("bass_protect_hz", [0.0, 80.0, 120.0, 180.0, 260.0]),
    ("conf_min", [0.0, 0.2, 0.35, 0.5, 0.7]),
    ("comb_fmax", [6000.0, 8000.0, 11000.0]),
]


def evaluate(mix, voc_ref, acc_ref, cfg, sr=44100, verbose=False):
    from vocal_split import VocalSplitter
    sp = VocalSplitter(sr=sr)
    sp.configure(**cfg)
    sp.reset()
    y = sp.process(mix, stems=False)
    n = min(len(y), len(voc_ref))
    est_v = y[:n, 0]
    est_a = y[:n, 1]

    # AI 参考：FPGA 的人声出自 mid，所以按 mid 比才公平
    rv = 0.5 * (voc_ref[:n, 0] + voc_ref[:n, 1])
    ra = 0.5 * (acc_ref[:n, 0] + acc_ref[:n, 1])

    m = {}
    m["sdr_v"] = si_sdr(est_v, rv)
    m["sdr_a"] = si_sdr(est_a, ra)
    m["sir_v"], m["cap_v"] = project_sir(est_v, rv, ra)
    m["sir_a"], m["cap_a"] = project_sir(est_a, ra, rv)
    m["score"] = m["sdr_v"] + m["sdr_a"]          # 综合分（越小越好）
    if verbose:
        print("    人声 %+6.2f dB | 伴奏 %+6.2f dB | 人声SIR %+6.2f | "
              "伴奏SIR %+6.2f | 捕获 %+6.2f dB"
              % (m["sdr_v"], m["sdr_a"], m["sir_v"], m["sir_a"], m["cap_v"]),
              flush=True)
    return m


def band_sdr(est, ref, sr, edges=(60, 120, 250, 500, 1000, 2000, 4000,
                                  6000, 9000, 13000, 20000)):
    """按倍频程带看 SI-SDR 掉在哪一段。

    做法：整段 FFT，用矩形带通掩蔽取出一段再 IFFT。
    比多组 IIR 简单，也不会引入相位失真（同一掩蔽同时作用在 est 和 ref 上，
    两者经过完全相同的滤波器，比较才公平）。
    """
    e = np.asarray(est, dtype=np.float64).ravel()
    r = np.asarray(ref, dtype=np.float64).ravel()
    n = min(e.size, r.size)
    e, r = e[:n], r[:n]
    E = np.fft.rfft(e)
    R = np.fft.rfft(r)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = ((f >= lo) & (f < hi)).astype(np.float64)
        if not m.any():
            continue
        ee = np.fft.irfft(E * m, n)
        rr = np.fft.irfft(R * m, n)
        rows.append((lo, hi, si_sdr(ee, rr),
                     10 * np.log10((float(np.sum(rr ** 2)) + 1e-20)
                                   / (float(np.sum(r ** 2)) + 1e-20))))
    return rows


def print_bands(est, ref, sr, title):
    print("    %s：按频带看 SI-SDR" % title)
    print("      %-14s %9s %10s" % ("频带", "SI-SDR", "参考能量占比"))
    for lo, hi, s, e in band_sdr(est, ref, sr):
        bar = "#" * int(max(0.0, min(30.0, s + 10.0)))
        print("      %4d-%5d Hz %+8.2f dB %9.1f%%  %s"
              % (lo, hi, s, 100.0 * (10 ** (e / 10.0)), bar), flush=True)


def fmt_row(tag, m, best=None):
    mark = ""
    if best is not None:
        if abs(m["score"] - best) < 1e-9:
            mark = "  <== 最好"
    print("  %-46s %+7.2f %+7.2f %+7.2f %+7.2f %+7.2f%s"
          % (tag, m["sdr_v"], m["sdr_a"], m["sir_v"], m["sir_a"],
             m["cap_v"], mark), flush=True)


def header():
    print("  %-46s %7s %7s %7s %7s %7s"
          % ("配置", "人声SDR", "伴奏SDR", "人声SIR", "伴奏SIR", "人声捕获"),
          flush=True)
    print("  " + "-" * 92, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", default="", help="非空则用 clip 参考（秒区间仅用于命名）")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--best", action="store_true")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--diag", action="store_true", help="输出按频带的误差分布")
    ap.add_argument("--secs", default="", help="切片秒区间，如 40:100（扫参数用）")
    ap.add_argument("--cfg", default="", help="额外参数覆盖（JSON），如 "
                                             "'{\"f0_whiten_hz\":0}'")
    ap.add_argument("--sr", type=int, default=24414,
                    help="评估采样率。24414 = FPGA/UART 实际帧率（默认）；"
                         "0 = 用参考文件原始采样率（44.1k，上位机通路）")
    args = ap.parse_args()

    sr_want = args.sr if args.sr > 0 else None
    secs = parse_secs(args.secs)
    mix, voc_ref, acc_ref, sr = load_refs(args.clip, sr_want, secs)
    n = len(mix)
    print("=" * 94)
    print(" FPGA 分离算法精校（基准 = htdemucs 在真实歌曲上的分离结果）")
    print("  参考：%s%s  (%d Hz, %.1f s)"
          % ("片段 " + args.secs + " s" if secs else
             ("片段" if args.clip else "整首"), "", sr, n / float(sr)))
    # 参考本身的性质：AI 人声有多少在 mid 之外（FPGA 结构上就拿不到）
    rv = 0.5 * (voc_ref[:, 0] + voc_ref[:, 1])
    sv = 0.5 * (voc_ref[:, 0] - voc_ref[:, 1])
    ev = float(np.sum(rv ** 2)) + 1e-20
    ss = float(np.sum(sv ** 2))
    print("  AI 人声的 side/mid 能量比 = %.1f%%  （超出中置的能量，"
          "M/S 类算法结构上取不到）" % (100.0 * ss / ev))
    ra = 0.5 * (acc_ref[:, 0] + acc_ref[:, 1])
    print("  AI 人声 / AI 伴奏 能量比 = %.1f dB"
          % (10 * np.log10((ev + 1e-20) / (float(np.sum(ra ** 2)) + 1e-20))))
    print("=" * 94)

    results = []

    if args.sweep:
        # ---- 第 1 轮：逐个参数扫，其余固定为 BASE，看边际效应 ----
        print("\n第 1 轮：单参数（其余 = 基线）", flush=True)
        header()
        chosen = {}
        for key, vals in GRIDS:
            print("  --- %s ---" % key, flush=True)
            rows = []
            for v in vals:
                cfg = dict(BASE)
                cfg[key] = v
                m = evaluate(mix, voc_ref, acc_ref, cfg, sr=sr)
                rows.append((v, m))
                fmt_row("%s=%g" % (key, v), m)
            best_v, best_m = max(rows, key=lambda t: t[1]["score"])
            chosen[key] = best_v
            print("    -> 最优 %s=%g（综合 %+.2f dB）"
                  % (key, best_v, best_m["score"]), flush=True)

        # ---- 第 2 轮：把各参数的最优值合起来，再逐个坐标下降 ----
        cfg2 = dict(BASE)
        cfg2.update(chosen)
        print("\n第 2 轮：合并第 1 轮最优值 + 坐标下降", flush=True)
        header()
        m2 = evaluate(mix, voc_ref, acc_ref, cfg2, sr=sr)
        fmt_row("合并 %s" % chosen, m2)

        for it in range(2):
            improved = False
            for key, vals in GRIDS:
                cur = cfg2[key]
                rows = []
                for v in vals:
                    c = dict(cfg2)
                    c[key] = v
                    m = evaluate(mix, voc_ref, acc_ref, c, sr=sr)
                    rows.append((v, m))
                bv, bm = max(rows, key=lambda t: t[1]["score"])
                if bm["score"] > m2["score"] + 1e-6:
                    cfg2[key] = bv
                    m2 = bm
                    improved = True
                    print("    第 %d 轮 %s %g -> %g（综合 %+.2f）"
                          % (it + 1, key, cur, bv, bm["score"]), flush=True)
            if not improved:
                break

        print("\n  最终参数：", flush=True)
        print("    " + json.dumps({k: cfg2[k] for k in sorted(cfg2)
                                   if cfg2[k] != BASE[k]},
                                  ensure_ascii=False), flush=True)
        print("\n  最终对比：", flush=True)
        header()
        fmt_row("基线 BASE", evaluate(mix, voc_ref, acc_ref, BASE, sr=sr))
        fmt_row("精校后", m2)
        m_base = evaluate(mix, voc_ref, acc_ref, BASE, sr=sr)
        print("\n  提升：人声 SDR %+.2f dB，伴奏 SDR %+.2f dB，综合 %+.2f dB"
              % (m2["sdr_v"] - m_base["sdr_v"], m2["sdr_a"] - m_base["sdr_a"],
                 m2["score"] - m_base["score"]), flush=True)

        print("\n  按综合分排序（Top %d）：" % args.top, flush=True)
        header()
        for cfg, m in sorted(results, key=lambda t: -t[1]["score"])[:args.top]:
            fmt_row(str(cfg), m)

    elif args.best:
        # 用 sweep 收敛出来的参数在整首上复核
        CFG = json.loads(os.environ.get("CALIB_CFG", "{}"))
        cfg = dict(BASE)
        cfg.update(CFG)
        header()
        m = evaluate(mix, voc_ref, acc_ref, cfg, sr=sr, verbose=False)
        fmt_row("最优参数 %s" % CFG, m)
        m0 = evaluate(mix, voc_ref, acc_ref, BASE, sr=sr)
        fmt_row("基线（原默认值）", m0)
        print("\n  提升：人声 SDR %+.2f dB，伴奏 SDR %+.2f dB，综合 %+.2f dB"
              % (m["sdr_v"] - m0["sdr_v"], m["sdr_a"] - m0["sdr_a"],
                 m["score"] - m0["score"]))

    elif args.cfg:
        # 单点 A/B：给定配置 vs 改版前基线 + 现在的默认值
        over = json.loads(args.cfg)
        cfg = dict(BASE)
        cfg.update(over)
        header()
        fmt_row("改版前（2026-10-21 之前的默认值）",
                evaluate(mix, voc_ref, acc_ref, BASE_OLD, sr=sr))
        fmt_row("现在默认（合围齿宽 + 基频谱白化）",
                evaluate(mix, voc_ref, acc_ref, BASE, sr=sr))
        fmt_row("叠加覆盖 %s" % over,
                evaluate(mix, voc_ref, acc_ref, cfg, sr=sr))

    else:
        header()
        m_old = evaluate(mix, voc_ref, acc_ref, BASE_OLD, sr=sr)
        fmt_row("改版前（2026-10-21 之前的默认值）", m_old)
        m0 = evaluate(mix, voc_ref, acc_ref, BASE, sr=sr)
        fmt_row("现在默认（梳齿 18 Hz + 基频谱白化 250 Hz）", m0)
        print("\n  本次精校的提升：人声 SDR %+.2f dB，伴奏 SDR %+.2f dB，"
              "综合 %+.2f dB"
              % (m0["sdr_v"] - m_old["sdr_v"], m0["sdr_a"] - m_old["sdr_a"],
                 m0["score"] - m_old["score"]))

        if args.diag:
            from vocal_split import VocalSplitter
            sp = VocalSplitter(sr=sr)
            sp.configure(**BASE)
            sp.reset()
            y = sp.process(mix)
            n2 = min(len(y), len(voc_ref))
            rv0 = 0.5 * (voc_ref[:n2, 0] + voc_ref[:n2, 1])
            ra0 = 0.5 * (acc_ref[:n2, 0] + acc_ref[:n2, 1])
            print()
            print_bands(y[:n2, 0], rv0, sr, "人声估计")
            print()
            print_bands(y[:n2, 1], ra0, sr, "伴奏估计")
            print()

        # 两个结构性的对照，说明"这套经典算法大概能到哪"
        print("\n  结构对照：", flush=True)
        cfg = dict(BASE)
        cfg["harmonic"] = False
        m1 = evaluate(mix, voc_ref, acc_ref, cfg, sr=sr)
        fmt_row("关掉谐波梳（纯 M/S 中置提取）", m1)

        cfg = dict(BASE)
        cfg["gate_db"] = 0.0          # 0 表示不做幅度门限
        m2 = evaluate(mix, voc_ref, acc_ref, cfg, sr=sr)
        fmt_row("关掉幅度门限", m2)

        # 理想上界：直接把 AI 人声的 mid 当输出（= 算法天花板参考）
        print("\n  参考上界（不是算法输出，只用来定标）：", flush=True)
        rv2 = 0.5 * (voc_ref[:, 0] + voc_ref[:, 1])
        ra2 = 0.5 * (acc_ref[:, 0] + acc_ref[:, 1])
        s, c = project_sir(rv2, rv2, ra2)
        print("    AI 人声的 mid 直接当输出：人声SIR %+6.2f dB，"
              "相对 AI 人声的 SI-SDR %+6.2f dB" % (s, si_sdr(rv2, rv2)), flush=True)

    print("=" * 94)
    return 0


if __name__ == "__main__":
    sys.exit(main())
