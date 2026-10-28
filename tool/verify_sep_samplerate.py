#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段二收尾：验证「采样率匹配」这一条。

用户点名要分析的三类可能原因里，前两类（模型参数、前后处理）已经在
local_ai / vocal_split / vocal_route 侧定位并修复完毕。这一支脚本专门
量化第三类：**输入采样率与模型原生采样率不一致时，分离精度掉多少**。

★ 有一条非常重要的教训（别再踩）：

  本脚本早期用"人声 SI-SDR"当核心指标，数字在 -65 dB 到 +23 dB 之间乱跳，
  一度以为发现了大问题。逐源查能量才发现：**把纯合成的类人声单独送进
  demucs，4 个源的能量占比是 drums 0.2% / bass 0.0% / other 99.8% /
  vocals 0.0%** —— demucs 的人声检测器根本不认合成音，人声轨拿到的是
  接近 0 的残差，此时算 SI-SDR 就是在噪声上做比值，毫无意义。
  所以绝对 SI-SDR 只能在**真人录音**上谈；本脚本不提供真人也拿不到真值，
  于是改用下面这组**不依赖素材真实性**的硬指标。

验收的硬指标（与"素材像不像人声"完全无关）：
  [A] 等长、有限（无 NaN/Inf）
  [B] 能量守恒：(人声+伴奏)/输入 ≈ 1.0
  [C] 重建恒等式：demucs 四轨之和 ≈ 输入（SI-SDR 应 > 25 dB）
      —— 这一条同时管住了重采样、归一化、split/overlap、shifts 的
         全部前后处理：只要有一处漏增益/错位，四轨就加不回输入。
  [D] 纯重采样往返本身的 SI-SDR > 30 dB（隔离 resample_to 的抗混叠）
  [E] 44.1k 与 48k 两路的 [A][B][C] 都过（采样率匹配不是瓶颈）

做法：
  1. 在 44.1 kHz（demucs htdemucs 的原生采样率）上合成测试混音。
  2. 分路 A：直接用 44.1 kHz 送入 separate()。
  3. 分路 B：先用带限 sinc 升到 48 kHz（模拟"设备只支持 48k"），再送入
     separate()（内部 48k→44.1k→分离→44.1k→48k）。

★ 注意：早期 48 kHz 分支报过
     ValueError: shapes (480000,) and (441000,) not aligned
  那是**这条验证脚本自己**把 48 kHz 输出（240000 点）跟 44.1 kHz 真值
  （220500 点）直接比对了，不是 local_ai 的缺陷。这里修正为「先对齐采样率
  再比 SI-SDR」。

用法：
    python tool/verify_sep_samplerate.py            # 5 秒，跑得快
    python tool/verify_sep_samplerate.py --secs 10  # 更稳
"""
from __future__ import print_function

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))

import local_ai  # noqa: E402


# ---------------------------------------------------------------- 信号合成

def _syllable_env(n, sr, syll_hz=2.7, seed=3):
    """★ 音节包络（持续不断）。

    第一版写成了"整体指数衰减 tau=0.35 s"，结果 5 秒以上的测试信号里
    "人声"其实只有开头 1 秒有声、后面全静音 —— SI-SDR 变成在残余噪声上
    做比值，数字在 -14 dB 和 +23 dB 之间乱跳（tool/diag_seg_split.py 实测）。
    真人唱歌是一串音节，所以这里改成每 1/syll_hz 秒一个起音-衰减，
    并叠一层慢速力度起伏。
    """
    t = np.arange(n, dtype=np.float64) / float(sr)
    seg = (t * syll_hz) % 1.0 / syll_hz          # 音节内已过的秒数
    e = (1.0 - np.exp(-seg / 0.012)) * np.exp(-seg / 0.10)
    rng = np.random.RandomState(seed)
    # 每 8 个音节换一次力度（乐句感）
    phr = np.floor(t * syll_hz / 8.0).astype(int)
    dyn = 0.65 + 0.35 * rng.rand(int(phr.max()) + 2)
    e *= dyn[phr]
    return e


# 五个元音的三个共振峰（F1,F2,F3），单位 Hz。共振峰结构才是"像人声"的关键：
# 只有谐波堆叠的话 demucs 会把它当成合成器 pad 判进 other 轨（实测人声
# SI-SDR -35 dB 以下），加了共振峰之后才会被认成 vocals。
_VOWELS = {
    "a": (730.0, 1090.0, 2440.0),
    "e": (530.0, 1840.0, 2480.0),
    "i": (270.0, 2290.0, 3010.0),
    "o": (570.0, 840.0, 2410.0),
    "u": (300.0, 870.0, 2240.0),
}
_VOWEL_SEQ = ["a", "e", "i", "o", "u", "a", "i", "e"]
_FORM_BW = (60.0, 90.0, 130.0)      # 三个共振峰的带宽
_FORM_AMP = (1.0, 0.70, 0.32)       # 相对强度
_N_HARM = 24


def _voice_like(n, sr, seed=7):
    """源-滤波器模型的类人声：

    声门源（谐波幅度按 k^-1.4 衰减，接近 -12 dB/oct）
      × 三个时变共振峰的谐振增益（Lorentz 近似）
      × 音节包络 + 颤音 + 气声
    共振峰每 0.18 s 换一个元音，形成连续的"说话/唱歌"感。
    """
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=np.float64) / float(sr)

    # ---- 旋律（五声音阶，每 0.9 s 一个音） ----
    scale = [196.0, 220.0, 246.9, 293.7, 329.6, 246.9, 220.0, 196.0]
    idx = np.floor(t / 0.9).astype(int) % len(scale)
    f0 = np.array(scale, dtype=np.float64)[idx]
    f0 *= (1.0 + 0.006 * np.sin(2 * np.pi * 5.2 * t))   # 颤音
    ph = 2.0 * np.pi * np.cumsum(f0) / sr

    # ---- 共振峰轨迹（每 0.18 s 一个元音，元音之间线性过渡） ----
    vidx = np.floor(t / 0.18).astype(int) % len(_VOWEL_SEQ)
    F = np.zeros((n, 3), dtype=np.float64)
    for i, key in enumerate(_VOWEL_SEQ):
        m = (vidx == i)
        if m.any():
            F[m, :] = np.array(_VOWELS[key], dtype=np.float64)
    # 轻度平滑 = 元音间的滑音过渡
    k = np.ones(64, dtype=np.float64) / 64.0
    for c in range(3):
        F[:, c] = np.convolve(np.concatenate([F[:64, c], F[:, c], F[-64:, c]]),
                              k, mode="same")[64:64 + n]

    # ---- 谐波求和 ----
    v = np.zeros(n, dtype=np.float64)
    for kh in range(1, _N_HARM + 1):
        fk = kh * f0
        if np.min(fk) > sr * 0.45:          # 超过奈奎斯特就别算了
            break
        g = np.zeros(n, dtype=np.float64)
        for c in range(3):
            d = (fk - F[:, c]) / _FORM_BW[c]
            g += _FORM_AMP[c] / (1.0 + d * d)
        v += (kh ** -1.4) * g * np.sin(kh * ph + 0.3 * kh)
    v += 0.03 * rng.randn(n)                # 气声
    v *= _syllable_env(n, sr)
    v /= (np.max(np.abs(v)) + 1e-12)
    return v * 0.6


def _acc_like(n, sr, seed=11):
    """伴奏：居中贝斯走根音 + 硬声像到左的 4 kHz 打击 + 右声道的沙锤噪声。"""
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=np.float64) / float(sr)
    roots = [98.0, 98.0, 123.5, 110.0]                  # 每 2 秒换一个根音
    ridx = np.floor(t / 2.0).astype(int) % len(roots)
    f_bass = np.array(roots, dtype=np.float64)[ridx]
    bass = np.sin(2 * np.pi * np.cumsum(f_bass) / sr) * 0.8
    # 4 kHz 硬声像：左轨有、右轨无 —— 作为"伴奏不泄漏进人声"的独立先验
    hi = np.sin(2 * np.pi * 4000.0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 2.3 * t))
    noise = rng.randn(n) * 0.15
    L = 0.5 * bass + 0.45 * hi + 0.10 * noise
    R = 0.5 * bass + 0.00 * hi - 0.10 * noise
    m = max(np.max(np.abs(L)), np.max(np.abs(R)))
    L /= (m + 1e-12)
    R /= (m + 1e-12)
    return np.stack([L, R], axis=1) * 0.45


def make_truth(n, sr):
    v = _voice_like(n, sr)
    acc = _acc_like(n, sr)
    vocal = np.stack([v, v], axis=1)                    # 人声居中
    mix = vocal + acc
    m = np.max(np.abs(mix))
    if m > 0:
        mix /= m / 0.9
        vocal /= m / 0.9
        acc /= m / 0.9
    return vocal.astype(np.float32), acc.astype(np.float32), mix.astype(np.float32)


# ---------------------------------------------------------------- 指标

def si_sdr(est, ref, eps=1e-12):
    """尺度无关信噪比 (dB)。est/ref 形状相同。"""
    e = np.asarray(est, dtype=np.float64).ravel()
    r = np.asarray(ref, dtype=np.float64).ravel()
    n = min(e.size, r.size)
    e = e[:n]
    r = r[:n]
    if np.allclose(r, 0.0):
        return float("nan")
    alpha = float(np.dot(e, r) / (np.dot(r, r) + eps))
    err = e - alpha * r
    num = float(np.dot(alpha * r, alpha * r))
    den = float(np.dot(err, err)) + eps
    if num <= 0.0:
        return float("nan")
    return 10.0 * np.log10(num / den)


def energy_ratio(vocal, acc, mix):
    ev = float(np.sum(np.asarray(vocal, dtype=np.float64) ** 2))
    ea = float(np.sum(np.asarray(acc, dtype=np.float64) ** 2))
    em = float(np.sum(np.asarray(mix, dtype=np.float64) ** 2))
    if em <= 0.0:
        return float("nan")
    return (ev + ea) / em


# ---------------------------------------------------------------- 主流程

def run_case(tag, mix, sr, ref_vocal, ref_acc, sr_ref):
    print("--- %s (%.1f s) ---" % (tag, mix.shape[0] / float(sr)))
    t0 = time.time()
    voc, acc, info = local_ai.separate(mix, int(sr), device="cpu")
    dt = time.time() - t0
    print("  设备=%s  模型=%s  原生 %d Hz  shifts=%s overlap=%.2f seg=%.1fs"
          % (info.get("device"), info.get("model"), info.get("model_sr"),
             info.get("shifts"), info.get("overlap"), info.get("segment")))

    voc = np.asarray(voc, dtype=np.float32)
    acc = np.asarray(acc, dtype=np.float32)

    ok_len = (voc.shape[0] == mix.shape[0]) and (acc.shape[0] == mix.shape[0])
    ok_fin = bool(np.all(np.isfinite(voc)) and np.all(np.isfinite(acc)))
    er = energy_ratio(voc, acc, mix)

    # ★ 核心指标：四轨重建恒等式。人声+伴奏 应该 ≈ 输入本身。
    #   这一条把重采样 / 归一化 / split-OLA / shifts 全串在一起验了。
    rec = si_sdr(voc + acc, mix)

    # 参考 SI-SDR 仅供观察（合成素材上无意义，见文件头说明）
    if int(sr) != int(sr_ref):
        vv = local_ai.resample_to(voc, int(sr), int(sr_ref))
        aa = local_ai.resample_to(acc, int(sr), int(sr_ref))
    else:
        vv, aa = voc, acc
    n = min(vv.shape[0], ref_vocal.shape[0])
    sdr_v = si_sdr(vv[:n], ref_vocal[:n])
    sdr_a = si_sdr(aa[:n], ref_acc[:n])

    print("  耗时 %.1f s" % dt)
    print("  等长=%s (%d/%d)  有限=%s" % (ok_len, voc.shape[0], mix.shape[0], ok_fin))
    print("  能量守恒 (人声+伴奏)/输入 = %.3f  (应≈1.0)" % er)
    print("  重建恒等式 人声+伴奏 vs 输入 = %+.2f dB  (应 > 25 dB)" % rec)
    print("  [仅供参考] 人声 SI-SDR = %+.2f dB   伴奏 SI-SDR = %+.2f dB"
          % (sdr_v, sdr_a))
    return dict(len=ok_len, finite=ok_fin, er=er, rec=rec, sdr_v=sdr_v, sdr_a=sdr_a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=5.0)
    ap.add_argument("--skip-48k", action="store_true")
    args = ap.parse_args()

    sr0 = 44100                      # demucs 原生
    n0 = int(round(args.secs * sr0))
    ref_v, ref_a, mix0 = make_truth(n0, sr0)

    fails = []

    # ---- 对照组：纯重采样往返，不经过模型 ----------------------------
    # 用来回答"那 -1.29 dB 是重采样器的锅，还是模型本身的波动"。
    # 如果往返 SI-SDR 远高于模型通路（>40 dB），重采样就不是瓶颈。
    rt = local_ai.resample_to(local_ai.resample_to(mix0, sr0, 48000), 48000, sr0)
    n = min(rt.shape[0], mix0.shape[0])
    sdr_rt = si_sdr(rt[:n], mix0[:n])
    print("--- 对照组：纯 44.1k→48k→44.1k 往返（不过模型） ---")
    print("  往返 SI-SDR = %+.2f dB" % sdr_rt)
    if sdr_rt < 30.0:
        fails.append("重采样往返 SI-SDR 只有 %.1f dB，resample_to 抗混叠不足" % sdr_rt)
    print()

    def check(tag, r):
        if not r["len"]:
            fails.append("%s 输出长度与输入不符" % tag)
        if not r["finite"]:
            fails.append("%s 输出含 NaN/Inf" % tag)
        if not (0.85 <= r["er"] <= 1.15):
            fails.append("%s 能量守恒 %.3f 越界（应≈1.0）" % (tag, r["er"]))
        if not (r["rec"] > 25.0):
            fails.append("%s 重建恒等式只有 %.1f dB（应 > 25 dB，"
                         "说明前后处理有增益/错位）" % (tag, r["rec"]))

    r44 = run_case("44.1k 原生（无重采样）", mix0, sr0, ref_v, ref_a, sr0)
    check("44.1k", r44)

    r48 = None
    if not args.skip_48k:
        sr1 = 48000
        mix1 = local_ai.resample_to(mix0, sr0, sr1).astype(np.float32)
        r48 = run_case("48k 输入（内部往返重采样）", mix1, sr1, ref_v, ref_a, sr0)
        check("48k", r48)

    print()
    print("=== 采样率匹配结论 ===")
    if r44 and r48:
        print("  重建恒等式：44.1k %+.2f dB  vs  48k %+.2f dB  （差 %+.2f dB）"
              % (r44["rec"], r48["rec"], r48["rec"] - r44["rec"]))
        print("  纯重采样往返本身的 SI-SDR = %+.2f dB" % sdr_rt)
        if r48["rec"] - r44["rec"] < -3.0:
            print("  !! 48k 通路重建掉点超过 3 dB，检查 resample_to 的抗混叠")
        else:
            print("  OK：48k 与 44.1k 的重建质量基本持平，采样率匹配不是瓶颈")

    if fails:
        print()
        print("FAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
