# -*- coding: utf-8 -*-
"""diag_fpga_sep.py —— 量化 rtl/separation_core.v 在真实歌曲上的漏音。

为什么要这个脚本
----------------
用户反馈"伴奏时段人声声道还有输出"。这句话对应一个可测的量：
**纯器乐段里，人声声道相对混音的电平**（越低越干净）。拿已知无人声的
段子（开头 0.6~20 s）当标尺，不需要任何假设。

做法：直接复用 `tool/sim_separation.py` 里已经标定过的定点函数
（fixed_analyze / fixed_acf / pick_peak / decide / build_comb），
逐帧跑，再把 mask*V 做 IFFT + 加窗 + 重叠相加，得到"人声声道"。

输出两张表：
  1. 每 2 秒：人声声道相对混音的电平、检出的基频、who
  2. 汇总：器乐段 vs 人声段的漏音对比

用法： python tool/diag_fpga_sep.py [wav] [--start 0.6 --secs 20]
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim_separation as S          # 定点参考模型

N, HOP, NBIN, FS = S.N, S.HOP, S.NBIN, S.FS
WIN = S.WIN.astype(np.float64) / 32767.0


def f0_q8_from_lag(lag):
    if lag <= 0:
        return 0
    return int(round((N << 8) / float(lag)))


def frame_masks(V_re, V_im, verbose=None):
    """复刻 RTL 的 判定 -> 梳状 -> 掩蔽，返回 (mask_v, who, lag_m, lag_f)。"""
    acf = S.fixed_acf(V_re, V_im)
    acf0 = int(acf[0])
    lag_m, pk_m = S.pick_peak(acf, S.LAG_M_LO, S.LAG_M_HI)
    lag_f, pk_f = S.pick_peak(acf, S.LAG_F_LO, S.LAG_F_HI,
                              ghosts=S.ghost_lags(lag_m, S.LAG_F_LO, S.LAG_F_HI),
                              tol=max(3, lag_m >> 4))
    who, lm, lf = S.decide(lag_m, pk_m, lag_f, pk_f, acf0,
                           V_re, V_im, acf)
    cmb_m = np.zeros(NBIN, dtype=np.int64)
    cmb_f = np.zeros(NBIN, dtype=np.int64)
    if who in (1, 3):
        S.build_comb(f0_q8_from_lag(lm), cmb_m)
    if who in (2, 3):
        S.build_comb(f0_q8_from_lag(lf), cmb_f)
    cmb = np.maximum(cmb_m, cmb_f)
    if verbose is not None:
        verbose.update(who=who, lm=lm, lf=lf,
                       f_m=(FS / lm if lm else 0), f_f=(FS / lf if lf else 0),
                       acf0=acf0, pk_m=pk_m, pk_f=pk_f)
    return cmb, who, lm, lf


def run(L, R, log_every=None):
    """逐帧跑完整链路，返回 (vocal, rows)。"""
    n_frames = (len(L) - N) // HOP
    acc = np.zeros(len(L) + N, dtype=np.float64)
    wsum = np.zeros(len(L) + N, dtype=np.float64)
    acc_c = np.zeros(len(L) + N, dtype=np.float64)   # 只做中置提取（不过梳状）
    rows = []
    for fi in range(n_frames):
        a = fi * HOP
        seg_l = L[a:a + N]
        seg_r = R[a:a + N]
        V_re, V_im = S.fixed_analyze(seg_l, seg_r)
        info = {}
        cmb, who, lm, lf = frame_masks(V_re, V_im, info)
        mask = cmb.astype(np.float64) / 2.0            # 0 / 0.5 / 1.0
        # 人声谱 = mask * V，补成共轭对称后 IFFT
        spec = np.zeros(N, dtype=np.complex128)
        spec[:NBIN] = (V_re + 1j * V_im) * mask
        spec[NBIN - 1:0:-1] = np.conj(spec[1:NBIN])
        y = np.fft.ifft(spec).real
        y *= WIN
        acc[a:a + N] += y
        spec_c = np.zeros(N, dtype=np.complex128)
        spec_c[:NBIN] = (V_re + 1j * V_im)
        spec_c[NBIN - 1:0:-1] = np.conj(spec_c[1:NBIN])
        acc_c[a:a + N] += np.fft.ifft(spec_c).real * WIN
        wsum[a:a + N] += WIN ** 2
        if log_every and fi % log_every == 0:
            e_mix = np.sqrt(np.mean((0.5 * (seg_l + seg_r)) ** 2))
            rows.append(dict(t=a / FS, who=who, f_m=info["f_m"], f_f=info["f_f"],
                             mix_db=20 * np.log10(max(e_mix, 1e-9) / 32768.0),
                             pk_m=info["pk_m"], acf0=info["acf0"]))
    wsum[wsum < 1e-9] = 1.0
    vocal = (acc / wsum)[:len(L)] * (2.0 / 1.5)        # 与 RTL 的 2/3 归一化一致
    center = (acc_c / wsum)[:len(L)] * (2.0 / 1.5)
    return vocal, center, rows


def db_rel(x, ref):
    e = np.sqrt(np.mean(x ** 2)) + 1e-12
    r = np.sqrt(np.mean(ref ** 2)) + 1e-12
    return 20 * np.log10(e / r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", default=None)
    ap.add_argument("--start", type=float, default=0.6)
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--chunk", type=float, default=2.0)
    a = ap.parse_args()

    src = a.wav or r"C:/Users/Administrator/Downloads/老狼&王婧_想把我唱给你听.flac"
    x, sr = sf.read(src, dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    # 参考模型按 24.414 kHz 标定，这里重采样到同一点
    if abs(sr - 24414.0) > 1.0:
        n_out = int(len(x) * 24414.0 / sr)
        idx = np.linspace(0, len(x) - 1, n_out)
        x = np.stack([np.interp(idx, np.arange(len(x)), x[:, c])
                      for c in range(2)], axis=1)
        sr = 24414.0
    L = (x[:, 0] * 32767.0).astype(np.int64)
    R = (x[:, 1] * 32767.0).astype(np.int64)

    a0 = int(a.start * sr)
    a1 = min(len(L), a0 + int(a.secs * sr))
    print("文件 %s  sr=%d  分析段 %.1f~%.1f s" %
          (os.path.basename(src), sr, a0 / sr, a1 / sr))
    vocal, center, rows = run(L[a0:a1], R[a0:a1],
                              log_every=int(a.chunk * sr / HOP))
    mid = 0.5 * (L[a0:a1] + R[a0:a1]).astype(np.float64)
    # 末尾 N 点没有被所有重叠窗覆盖，wsum 很小 → 除法会把尾巴放大成尖峰。
    # 只统计被完整覆盖的区间（这是上一版 +14 dB 假峰的来源）。
    cov = len(vocal) - N

    print("\n t(s)   who  男f0  女f0   混音dB   中置提取   过梳状后")
    for r in rows:
        seg0 = int(r["t"] * sr)
        seg1 = min(cov, seg0 + int(a.chunk * sr))
        if seg1 - seg0 < sr // 4:
            continue
        c_rel = db_rel(center[seg0:seg1], mid[seg0:seg1])
        v_rel = db_rel(vocal[seg0:seg1], mid[seg0:seg1])
        print("%5.1f    %d  %5.0f %5.0f  %6.1f   %+7.1f   %+7.1f dB" %
              (r["t"], r["who"], r["f_m"], r["f_f"], r["mix_db"], c_rel, v_rel))

    print("\n整段：中置提取 %+.1f dB / 过梳状后 %+.1f dB（相对混音）"
          % (db_rel(center[:cov], mid[:cov]), db_rel(vocal[:cov], mid[:cov])))
    who_cnt = {w: sum(1 for r in rows if r["who"] == w) for w in (0, 1, 2, 3)}
    print("判定分布（每 %.1f s 一次抽样）：无=%d 男=%d 女=%d 两人=%d"
          % (a.chunk, who_cnt[0], who_cnt[1], who_cnt[2], who_cnt[3]))


if __name__ == "__main__":
    main()
