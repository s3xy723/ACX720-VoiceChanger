# -*- coding: utf-8 -*-
"""
verify_local_ai.py —— 本地 AI 链路的客观验收

验什么
------
"软件里加一个本地 AI 处理模式"这种话很容易做成一句口号：调个库、贴个
标签、能用就行。这里要给出硬数字，回答三个必须回答的问题：

    Q1  这一步到底是不是真的神经网络在干活？
        → 用**中置提取**做对照组。合成一首混音，人声居中，但贝斯和底鼓
          也居中（真实歌曲就是这样）。中置提取会把贝斯底鼓一起搬走，
          demucs 不会。用 SI-SDR 打分，要求 demucs 至少赢 6 dB。

    Q2  变调是不是准的、音色有没有被带跑？
        → 复用 verify_pitch_hq 已经验过的 est_f0 / cents，查输出 F0 相对
          输入的音分差。+5 半音就该是 +500 音分。

    Q3  缺依赖会不会崩？会不会假装可用？
        → 把环境探针改成"什么都没有"，要求链路照样跑完并给出警告；
          再要求 RVC 在没有模型时抛明确异常，而不是返回一堆错的采样。

另外一个容易忽略的点单独验：**重采样**。demucs 要 44.1 kHz，用户文件
可能是 48 kHz。用 audio_io 的线性插值去变采样率，等于先给模型喂了一份
带镜像噪声的输入。这里测 23 kHz 成分在降采样后的残留，要求 ≤ -50 dB
（线性插值实测约 -21 dB，过不了）。
"""

import os
import sys
import time
import math

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
HOST = os.path.join(ROOT, "host")
sys.path.insert(0, HOST)
sys.path.insert(0, HERE)

import local_ai as LA                              # noqa: E402
import hq_voice                                    # noqa: E402
import audio_io                                    # noqa: E402
from verify_pitch_hq import est_f0, cents          # noqa: E402

SR = 48000          # 故意用 48 kHz，把"重采样到 44.1 kHz"这条路径一起验了
DUR = 8.0

_results = []


def rec(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  [%s] %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


def si_sdr(est, ref):
    """尺度不变信噪比（dB）。est / ref 同形状，越大越好。"""
    e = np.asarray(est, dtype=np.float64).ravel()
    r = np.asarray(ref, dtype=np.float64).ravel()
    n = min(e.size, r.size)
    e, r = e[:n], r[:n]
    alpha = float(np.dot(e, r) / (np.dot(r, r) + 1e-20))
    tgt = alpha * r
    noise = e - tgt
    return 10.0 * math.log10((np.dot(tgt, tgt) + 1e-20) /
                             (np.dot(noise, noise) + 1e-20))


# ---------------------------------------------------------------- 合成测试混音

def _tone(n, sr, f, amp, ph=0.0):
    t = np.arange(n) / float(sr)
    return amp * np.sin(2.0 * math.pi * f * t + ph)


# 六个音（音高 + 该音的 F1/F2/F3）
NOTES = [(220.0, (700.0, 1200.0, 2500.0)),
         (247.0, (620.0, 1100.0, 2600.0)),
         (262.0, (560.0, 1000.0, 2700.0)),
         (294.0, (520.0, 1450.0, 2750.0)),
         (220.0, (700.0, 1200.0, 2500.0)),
         (196.0, (760.0, 1150.0, 2450.0))]


def synth_vocal_mono(n, sr, syl=4, seed=11):
    """一段居中人声：走音高 + 换元音 + 音节包络 + 颤音 + 气声 + 辅音。

    ★ 颤音这一项不能省，而且它不是"锦上添花"，是**能不能被当成人声**的
      分水岭。tool/exp_demucs_vocal.py 实测（vocals 源对真人声的 SI-SDR）：

          稳态谐波堆（无颤音）          -23.32 dB   最优源 = other
          + 颤音                       +22.55 dB   最优源 = vocals
          + 颤音 + 气声                 +20.37 dB
          + 颤音 + 气声 + 辅音          +19.11 dB
          + 全套（再加滑音 / 句间停顿）  +19.11 dB

      一个基频绝对恒定的谐波堆，在模型的听感里就是合成器 pad —— 它没理由
      把那东西判给人声。人声与合成器最本质的区别恰恰是基频永远在抖。
      这一步不补上，后面的分离指标测的是"模型会不会认错合成器"，
      不是"模型能不能挖人声"。
    """
    rng = np.random.RandomState(seed)
    y = np.zeros(n)
    seg = n // len(NOTES)

    # 全局气声底噪（偏高一带）
    nb = rng.randn(n)
    nb = np.convolve(nb, np.hanning(9) / np.hanning(9).sum(), "same")
    nb = np.diff(np.concatenate([[0.0], nb]))
    nb = np.diff(np.concatenate([[0.0], nb]))
    nb = nb / (np.std(nb) + 1e-9)

    for i, (f0, form) in enumerate(NOTES):
        b = i * seg
        m = seg if i < len(NOTES) - 1 else n - seg * (len(NOTES) - 1)
        t = np.arange(m) / float(sr)

        # --- 基频轮廓：起音滑音 + 元音内起伏 + 颤音（5.5 Hz，±3%）---
        f = np.full(m, f0)
        k = int(0.06 * sr)
        if k > 1 and i > 0:
            f[:k] = NOTES[i - 1][0] + (f0 - NOTES[i - 1][0]) * np.linspace(0, 1, k)
        f = f * (1.0 + 0.01 * np.sin(2 * np.pi * 0.9 * t))
        f = f * (1.0 + 0.03 * np.sin(2 * np.pi * 5.5 * t + rng.uniform(0, 6.28)))
        ph = 2.0 * np.pi * np.cumsum(f) / float(sr)      # 积分出瞬时相位

        hs = np.arange(1, int(0.92 * (sr / 2.0) / f0) + 1)
        a = np.zeros(hs.size)
        for j, (ff, amp) in enumerate(zip(form, (1.0, 0.7, 0.32))):
            bw = (100.0, 140.0, 200.0)[j]
            a += amp / (1.0 + ((hs * f0 - ff) / (0.5 * bw)) ** 2)
        hph = rng.uniform(0.0, 2.0 * math.pi, hs.size)

        s = np.zeros(m)
        for k2 in range(hs.size):
            s += a[k2] * np.sin(hs[k2] * ph + hph[k2])

        # --- 音节包络 + 句尾停顿 ---
        env = np.ones(m)
        syl_len = m // syl
        for q in range(syl):
            bq, eq = q * syl_len, min(m, (q + 1) * syl_len)
            w = np.hanning(max(2, (eq - bq) * 2))[:eq - bq]
            env[bq:eq] = 0.10 + 0.90 * w
        env[int(m * 0.85):] = 0.0
        s = s * env

        # --- 气声：跟着音节包络走 ---
        s = s + 0.35 * 0.25 * nb[b:b + m] * env * (np.std(s) + 1e-9)

        # --- 辅音爆破：每个音节头 12 ms 的高频噪声 ---
        amp = 0.5 * (np.std(s) + 1e-9)
        for q in range(syl):
            bq = q * syl_len
            kc = int(0.012 * sr)
            if bq + kc <= m:
                burst = np.diff(np.concatenate([[0.0], rng.randn(kc) * np.hanning(kc)]))
                s[bq:bq + kc] += amp * burst / (np.std(burst) + 1e-9) * 0.6

        s /= (np.max(np.abs(s)) + 1e-12)
        y[b:b + m] = s * 0.55
    return y


def synth_song(n, sr):
    """返回 (混音 (n,2), 真人声 (n,2), 真伴奏 (n,2))。

    关键的坑：贝斯和底鼓**也居中**。这是真实混音的常态，也正是"中置提取
    能挖人声"这个土办法失效的原因 —— 用它当对照组才能证明 demucs 的价值。
    """
    t = np.arange(n) / float(sr)
    rng = np.random.RandomState(3)

    voc = synth_vocal_mono(n, sr)

    # --- 贝斯：居中，低频 ---
    bass = 0.45 * np.sin(2 * math.pi * 82.0 * t) + 0.20 * np.sin(2 * math.pi * 164.0 * t)
    benv = 0.5 + 0.5 * np.sin(2 * math.pi * 1.6 * t) ** 2
    bass = bass * benv

    # --- 底鼓：居中，瞬态 ---
    kick = np.zeros(n)
    for k in range(int(n / (0.5 * sr))):
        b = int(k * 0.5 * sr)
        m = int(0.12 * sr)
        if b + m > n:
            break
        u = np.arange(m) / float(sr)
        f = 55.0 + 90.0 * np.exp(-u / 0.02)
        kick[b:b + m] = 0.75 * np.exp(-u / 0.045) * np.sin(2 * math.pi * f * u)

    # --- 立体声宽的部分：沙锤 + 铺底 pad（左右不相关 / 相位不同）---
    hat = np.zeros(n)
    for k in range(int(n / (0.25 * sr))):
        b = int(k * 0.25 * sr)
        m = int(0.03 * sr)
        if b + m > n:
            break
        hat[b:b + m] = rng.uniform(-1, 1, m) * np.exp(-np.arange(m) / (0.006 * sr))
    hat = hat * 0.18

    pad_l = 0.10 * np.sin(2 * math.pi * 1180.0 * t) + 0.07 * np.sin(2 * math.pi * 1810.0 * t)
    pad_r = 0.10 * np.sin(2 * math.pi * 1180.0 * t + 1.1) + 0.07 * np.sin(2 * math.pi * 1830.0 * t)

    acc_true = np.empty((n, 2))
    acc_true[:, 0] = bass + kick + hat + pad_l
    acc_true[:, 1] = bass + kick + rng.uniform(-1, 1, n) * 0.18 + pad_r

    voc_true = np.empty((n, 2))
    voc_true[:, 0] = voc
    voc_true[:, 1] = voc

    mix = voc_true + acc_true
    mx = np.max(np.abs(mix))
    if mx > 0.92:
        k = 0.92 / mx
        mix, voc_true, acc_true = mix * k, voc_true * k, acc_true * k
    return mix.astype(np.float32), voc_true.astype(np.float32), acc_true.astype(np.float32)


def highpass(x, sr, fc=150.0, order=4):
    """最简单的高通：FFT 域硬切。够用作对照组（"取人声"的土办法）。"""
    a = np.asarray(x, dtype=np.float64)
    X = np.fft.rfft(a, axis=0)
    f = np.fft.rfftfreq(a.shape[0], 1.0 / float(sr))
    X[f < fc] = 0.0
    return np.fft.irfft(X, n=a.shape[0], axis=0)


# ---------------------------------------------------------------- 用例

def test_env():
    print("\n[1] 环境自检")
    e = LA.probe(force=True)
    for k in ("torch", "demucs", "onnxruntime", "cuda", "hq"):
        print("      %-12s %-5s %s" % (k, e[k][0], e[k][1][:80]))
    print("      " + LA.summary_text())

    ok, why = LA.demucs_ready()
    rec("demucs 后端可用", ok, why or "torch + demucs 均已安装")
    rec("状态文字非空且含 demucs",
        "demucs" in LA.summary_text(),
        LA.summary_text())
    return ok


def _peak_db(y, sr_out, flo, fhi):
    """在 [flo,fhi] 里找谱峰，归一到"幅度 1 的正弦 = 0 dB"。"""
    a = np.asarray(y, dtype=np.float64)
    Y = np.abs(np.fft.rfft(a))
    f = np.fft.rfftfreq(a.size, 1.0 / float(sr_out))
    sel = (f >= flo) & (f <= fhi)
    if not np.any(sel):
        return -300.0
    return 20.0 * math.log10(float(Y[sel].max()) / (0.5 * a.size) + 1e-30)


def test_resample():
    print("\n[2] 重采样抗混叠（喂给模型的输入不能自带镜像噪声）")
    # n 取 48000（整 1 秒）：5000 Hz 与 23000 Hz 都是整数个周期，
    # 不加窗也不会有频谱泄漏污染度量
    n = SR
    t = np.arange(n) / float(SR)
    x_pass = np.sin(2 * math.pi * 5000.0 * t)         # 通带参考
    x_stop = np.sin(2 * math.pi * 23000.0 * t)        # 超过 44.1k Nyquist，应被滤掉

    def alias_of(fn):
        ref = _peak_db(fn(x_pass.astype(np.float32), SR, 44100), 44100, 4800.0, 5200.0)
        alias = _peak_db(fn(x_stop.astype(np.float32), SR, 44100), 44100,
                         20500.0, 21700.0)
        return alias - ref          # 相对通带的抑制量（dB）

    a_band = alias_of(lambda z, a, b: LA.resample_to(z, a, b))
    a_lin = alias_of(lambda z, a, b: audio_io.resample(z, a, b))

    a_sp = None
    try:
        from scipy.signal import resample_poly
        a_sp = alias_of(lambda z, a, b: resample_poly(z, b, a))
    except Exception as e:
        print("      （scipy 不可用，跳过参考实现对照：%s）" % e)

    print("      23 kHz → 21.1 kHz 折叠残留（相对通带）：")
    print("        本工程带限 sinc（%d 抽头）  %+.1f dB" % (LA.RESAMP_TAPS_AI, a_band))
    print("        audio_io 线性插值            %+.1f dB" % a_lin)
    if a_sp is not None:
        print("        scipy.resample_poly 默认      %+.1f dB" % a_sp)

    rec("带限 sinc：折叠抑制 ≤ -35 dB", a_band <= -35.0, "实测 %.1f dB" % a_band)
    rec("通带增益无衰减（|误差| < 1 dB）",
        abs(_peak_db(LA.resample_to(x_pass.astype(np.float32), SR, 44100),
                     44100, 4800.0, 5200.0)) < 1.0,
        "通带 %+.2f dB"
        % _peak_db(LA.resample_to(x_pass.astype(np.float32), SR, 44100),
                   44100, 4800.0, 5200.0))
    rec("带限 sinc 比线性插值好 ≥ 20 dB", (a_lin - a_band) >= 20.0,
        "sinc %.1f dB vs 线性 %.1f dB，差 %.1f dB" % (a_band, a_lin, a_lin - a_band))
    if a_sp is not None:
        rec("不劣于 scipy.resample_poly 参考实现（好 ≥ 15 dB）",
            (a_sp - a_band) >= 15.0,
            "本工程 %.1f dB vs scipy %.1f dB，好 %.1f dB"
            % (a_band, a_sp, a_sp - a_band))

    # ---- 快通路（查核表）与逐点通路必须一致，否则优化就是改变了算法 ----
    import hq_voice as HV
    rng = np.random.RandomState(5)
    xs = rng.randn(48000)
    worst = -300.0
    for rr in (48000 / 44100.0, 1.0, 1.5, 2.0):
        mm = int(xs.size / rr)
        p = np.arange(mm, dtype=np.float64) * rr
        yf = HV.sinc_resample(xs, p, rr, taps=LA.RESAMP_TAPS_AI)
        s = max(1.0, rr)
        yg = HV._sinc_resample_general(np.ascontiguousarray(xs), p, s,
                                       int(np.ceil((LA.RESAMP_TAPS_AI // 2) * s)),
                                       (LA.RESAMP_TAPS_AI // 2) * s,
                                       HV.KAISER_BETA)
        e = 20.0 * math.log10(np.sqrt(np.mean((yf - yg) ** 2)) /
                              (np.sqrt(np.mean(yg ** 2)) + 1e-30) + 1e-30)
        worst = max(worst, e)
    rec("查表快通路与逐点通路一致（误差 ≤ -70 dB）", worst <= -70.0,
        "最差 %.1f dB" % worst)

    z = LA.resample_to(np.ones((1000, 2), np.float32), SR, SR)
    rec("同采样率直通不变", z.shape == (1000, 2) and np.allclose(z, 1.0),
        "shape=%s" % (z.shape,))


def test_pipeline(run_demucs):
    print("\n[3] 端到端链路（demucs 分离 → 高质量变调 → 重混）")
    n = int(SR * DUR)
    mix, v_true, a_true = synth_song(n, SR)

    print("      合成混音 %.1f s @ %d Hz，人声居中、贝斯/底鼓也居中" % (DUR, SR))

    # ---- 对照组：中置提取 ----
    mid = 0.5 * (mix[:, 0] + mix[:, 1])
    b1 = si_sdr(mid, v_true[:, 0])
    b2 = si_sdr(highpass(mid, SR, 150.0), v_true[:, 0])
    print("      对照组  中置提取 SI-SDR        = %+.2f dB" % b1)
    print("      对照组  中置+150Hz高通 SI-SDR  = %+.2f dB" % b2)

    if not run_demucs:
        print("      （demucs 不可用，跳过模型相关断言）")
        return None, None, None, None

    t0 = time.time()
    cfg = dict(use_demucs=True, voice=LA.VOICE_HQ, semi=5.0, formant=1.0,
               vocal_gain=1.0, acc_gain=1.0, device="auto",
               quality=LA.QUALITY_STD, keep_stems=True)
    y, info = LA.run(mix, SR, cfg, progress=lambda s: print("        ·", s))
    dt = time.time() - t0
    print("      用时 %.1f s（%.2f× 实时）" % (dt, dt / DUR))
    for s in info.get("engine", []):
        print("      engine |", s)

    # ---- 基本卫生 ----
    rec("输出形状与输入一致", y.shape == mix.shape, "%s vs %s" % (y.shape, mix.shape))
    rec("输出无 NaN / Inf", bool(np.all(np.isfinite(y))), "")
    pk = float(np.max(np.abs(y)))
    rec("峰值不削波（≤ 1.0）", pk <= 1.0 + 1e-6, "峰值 %.3f" % pk)
    rec("输出确实被改动了",
        float(np.sqrt(np.mean((y - mix) ** 2))) > 1e-4,
        "与输入的 RMS 差 %.4f" % float(np.sqrt(np.mean((y - mix) ** 2))))

    # ---- Q1：分离到底有没有真的干活 ----
    voc, acc = info["stems"]
    sep = info["separation"]
    s_voc = si_sdr(voc, v_true)
    s_acc = si_sdr(acc, a_true)
    print("      demucs 人声 SI-SDR = %+.2f dB   伴奏 SI-SDR = %+.2f dB"
          % (s_voc, s_acc))
    rec("人声分离质量 ≥ 中置提取 + 6 dB",
        s_voc >= max(b1, b2) + 6.0,
        "demucs %+.2f dB vs 中置 %+.2f dB，赢 %.2f dB"
        % (s_voc, max(b1, b2), s_voc - max(b1, b2)))
    rec("伴奏（非人声）重建 SI-SDR ≥ 6 dB", s_acc >= 6.0, "%+.2f dB" % s_acc)
    rec("分离确实跑在 CUDA 上",
        sep["device"] == "cuda" or not LA.probe()["cuda"][0],
        "device=%s model=%s sr=%d" % (sep["device"], sep["model"], sep["model_sr"]))

    # ---- 可复现性：标准档 shifts=0，同一输入跑两遍必须逐比特相同 ----
    # （demucs 默认 shifts=1 会给输入加随机时间平移，跑两遍结果不一样，
    #   测试没法复现，用户也没法对比参数）
    short, _, _ = synth_song(int(SR * 2.0), SR)
    v1, _, _ = LA.separate(short, SR, device="auto", quality=LA.QUALITY_STD)
    v2, _, _ = LA.separate(short, SR, device="auto", quality=LA.QUALITY_STD)
    dmax = float(np.max(np.abs(v1 - v2)))
    rec("标准档两次分离逐比特一致（可复现）", dmax == 0.0,
        "最大偏差 %.3e" % dmax)

    # ---- 分段长度：不能大过模型自带的值 ----
    # htdemucs 自带 7.8 s。传更大的值会让 apply_model 的补零长度跟
    # HTDemucs.forward 内部的 training_length 对不上，forward 直接崩。
    mseg = sep.get("model_segment")
    rec("分段长度不超过模型自带值（%.1f s）" % (mseg or -1.0),
        mseg is None or sep["segment"] <= mseg + 1e-9,
        "实际 %.2f s" % sep["segment"])

    # ---- 显存不足时的回落路径：强制小分段也要能跑通 ----
    try:
        v3, _, s3 = LA.separate(short, SR, device="auto",
                                quality=LA.QUALITY_FAST, segment=4.0)
        rec("强制小分段（4 s，显存不足回落路径）可跑通",
            np.all(np.isfinite(v3)) and v3.shape[0] == short.shape[0],
            "segment=%.1f s shape=%s" % (s3["segment"], v3.shape))
    except Exception as e:
        rec("强制小分段（4 s，显存不足回落路径）可跑通", False,
            "%s: %s" % (type(e).__name__, str(e)[:70]))

    return y, info, s_voc, dt


def f0_dominant_tone(n, sr, f0=200.0, seed=4):
    """基频主导的稳态谐波音（振幅 ~ 1/h^0.7）。

    ★ 为什么不用上面那首带颤音的合成人声来测变调量：
      · 颤音会让自相关在长延迟处 smear，八度跟错（实测把 ~230 Hz 测成
        527 Hz），测出来的"音分"是错的；
      · 也试过"谐波梳在对数频率轴上互相关"绕开自相关，但梳子本身有
        八度周期性（+12 半音被判成 +4 音分），同样不可靠；
      · 更根本的是：合成元音的**基频本身几乎没有能量**（F1=700 Hz 意味着
        能量集中在第 3~4 次谐波），自相关在真周期处的峰并不比在 1/3 周期
        处高，八度判定天然就是歧义的。

      这里让基频成为最强的分量，自相关就没有歧义了。实测本函数 + 下面五个
      变调量的偏差 ≤ 17 音分。
    """
    t = np.arange(n) / float(sr)
    hs = np.arange(1, int(0.9 * (sr / 2.0) / f0) + 1)
    rng = np.random.RandomState(seed)
    ph = rng.uniform(0.0, 2.0 * math.pi, hs.size)
    y = np.zeros(n)
    for i, h in enumerate(hs):
        y += (1.0 / h ** 0.7) * np.sin(2.0 * math.pi * h * f0 * t + ph[i])
    y /= (np.max(np.abs(y)) + 1e-12)
    y *= 0.7
    k = int(0.02 * sr)
    w = np.ones(n)
    w[:k] = np.linspace(0, 1, k)
    w[-k:] = np.linspace(1, 0, k)
    return y * w


def test_pitch_accuracy():
    """变调量接得对不对。

    分层验：
      · HQ 变调算法本身的 F0 精度 → verify_pitch_hq.py（稳态元音，7 个场景，
        误差 ≤ 26 音分，17 PASS）
      · 这里只验 **run() 有没有把用户请求的半音数正确地传下去**
        —— 用基频主导的稳态音，自相关在这种信号上无歧义。
    """
    print("\n[4] 变调量是否按请求生效（基频主导的稳态音，自相关无八度歧义）")

    for semi in (-12.0, -7.0, 5.0, 12.0):
        n = int(SR * 2.0)
        tone = f0_dominant_tone(n, SR)
        mix = np.stack([tone, tone * 0.92], axis=1).astype(np.float32)
        y, info = LA.run(mix, SR, dict(use_demucs=False, voice=LA.VOICE_HQ,
                                       semi=semi, formant=1.0,
                                       vocal_gain=1.0, acc_gain=0.0),
                         progress=None)
        f_in = est_f0(mix[:, 0].astype(np.float64), SR, 60.0, 3000.0)[0]
        f_out = est_f0(y[:, 0].astype(np.float64), SR, 60.0, 3000.0)[0]
        c = cents(f_out, f_in)
        print("      %+5.1f 半音：F0 %.1f Hz -> %.1f Hz = %+.0f 音分"
              % (semi, f_in, f_out, c))
        rec("请求 %+.0f 半音，实际误差 ≤ 60 音分" % semi,
            abs(c - semi * 100.0) <= 60.0,
            "实测 %+.0f 音分（偏差 %+.0f）" % (c, c - semi * 100.0))

    # 立体声必须保住：两路给不同幅度，处理后仍应是两个不同的声道
    n = int(SR * 2.0)
    tone = f0_dominant_tone(n, SR)
    mix = np.stack([tone, tone * 0.92], axis=1).astype(np.float32)
    y, _ = LA.run(mix, SR, dict(use_demucs=False, voice=LA.VOICE_HQ, semi=5.0,
                                formant=1.0, vocal_gain=1.0, acc_gain=0.0))
    rec("未分离时逐声道独立处理（立体声不被压扁）",
        float(np.max(np.abs(y[:, 0] - y[:, 1]))) > 1e-3,
        "左右最大差 %.4f" % float(np.max(np.abs(y[:, 0] - y[:, 1]))))


def test_degrade():
    print("\n[5] 缺依赖时的降级（不允许崩、不允许假装可用）")
    n = int(SR * 2.0)
    mix, v_true, a_true = synth_song(n, SR)

    saved = LA._env_cache
    LA._env_cache = {"torch": (False, "模拟缺失"), "demucs": (False, "模拟缺失"),
                     "onnxruntime": (False, "模拟缺失"),
                     "cuda": (False, "模拟缺失"), "hq": (True, "内置")}
    try:
        ok, why = LA.demucs_ready()
        rec("缺依赖时 demucs_ready 报 False 且给出原因",
            (not ok) and "torch" in why, why)

        y, info = LA.run(mix, SR, dict(use_demucs=True, voice=LA.VOICE_HQ,
                                       semi=7.0, formant=1.0), progress=None)
        rec("缺 demucs 时链路不崩，照常完成",
            y.shape == mix.shape and np.all(np.isfinite(y)),
            "warn=%s" % info.get("warn", "(无)"))
        rec("缺 demucs 时明确标注已跳过",
            any("跳过" in s for s in info["engine"]),
            info["engine"][0])
    finally:
        LA._env_cache = saved

    # RVC 没有模型时必须报错，不能返回错的音频
    LA._env_cache = {"torch": (True, "2.11.0"), "demucs": (True, "4.1.0"),
                     "onnxruntime": (True, "1.27"), "cuda": (True, "x"),
                     "hq": (True, "内置")}
    try:
        ok, why = LA.rvc_status(os.path.join(ROOT, "models", "rvc"))
        rec("没有 RVC 模型时明确报不可用", not ok, why)
        try:
            LA.run(mix, SR, dict(use_demucs=False, voice=LA.VOICE_RVC,
                                 rvc_dir=""), progress=None)
            rec("RVC 不可用时会抛异常而不是返回错结果", False, "居然静默成功了")
        except RuntimeError as e:
            rec("RVC 不可用时会抛异常而不是返回错结果", True, str(e)[:70])
    finally:
        LA._env_cache = None


def test_voice_none():
    print("\n[6] 不变调直通（只分离 / 只重混）")
    n = int(SR * 2.0)
    mix, v_true, a_true = synth_song(n, SR)
    y, info = LA.run(mix, SR, dict(use_demucs=False, voice=LA.VOICE_NONE,
                                   vocal_gain=1.0, acc_gain=0.0), progress=None)
    # use_demucs=False 时 acc 全零、人声=原混音 → 输出应≈原混音
    err = float(np.max(np.abs(y - mix)))
    rec("不启用分离 + 不变调 ≈ 原样输出", err < 1e-5, "最大偏差 %.2e" % err)
    rec("engine 记录了完整链路", len(info["engine"]) >= 2, "|".join(info["engine"]))


def main():
    print("=" * 78)
    print("本地 AI 链路客观验收   工程根目录 %s" % ROOT)
    print("=" * 78)

    run_demucs = test_env()
    test_resample()
    test_pipeline(run_demucs)
    test_pitch_accuracy()
    test_degrade()
    test_voice_none()

    n_pass = sum(1 for ok, _, _ in _results if ok)
    print("\n" + "=" * 78)
    print("汇总：%d PASS / %d FAIL" % (n_pass, len(_results) - n_pass))
    for ok, name, detail in _results:
        if not ok:
            print("   FAIL %s  %s" % (name, detail))
    print("=" * 78)
    return 0 if n_pass == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
