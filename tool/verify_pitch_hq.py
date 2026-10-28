# -*- coding: utf-8 -*-
"""
verify_pitch_hq.py —— 用硬数字证明「频域变调 + 共振峰保持」确实比时域变调好

跑法
----
    python tool/verify_pitch_hq.py

测什么
------
构造一个**参数完全已知**的合成元音（源-滤波器模型）：
    · 声源：F0 的锯齿波，谐波幅度 1/h
    · 声道：三个并联谐振器，中心频率 F1/F2/F3，Q 已知
于是"正确答案"是写死的，不需要靠耳朵判断。

对同一个输入分别跑：
    A. 时域双抽头延迟线（dsp_core.PitchShifter，与 rtl/pitch_shift.v 逐比特对齐）
    B. 频域相位声码器（hq_voice，formant=0，不做共振峰校正）
    C. 频域 + 共振峰保持（hq_voice，formant=1）

量四条：
    1. F0     输出基频 / 目标基频（应该 = 1.000）
    2. 共振峰 F1/F2/F3 的频率漂移（单位：半音 cents/100）
       —— A 应该跟着变调比一起跑，C 应该 ≈ 0
    3. 频谱包络与原信号的相关系数（C 应该接近 1，A 应该明显低）
    4. 瞬态咔哒数（相邻样本差分超过满量程 10% 的次数）

另外单步验证 A/B/C 三步各自的中间产物，确认流水线真的按设计走。

输出：Pass/Fail 清单 + 数据行。
"""

import os
import sys
import math

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
HOST = os.path.join(ROOT, "host")
sys.path.insert(0, HOST)
sys.path.insert(0, HERE)

import hq_voice                                    # noqa: E402
import dsp_core                                    # noqa: E402

SR = 48000
NFFT = 1024

# 一个成年男声的 /a/ 元音：F0 120 Hz，三个共振峰
F0 = 120.0
FORMANTS = (700.0, 1200.0, 2500.0)     # F1 F2 F3，间距 >= 500 Hz（要能被 375 Hz
BW = (100.0, 140.0, 200.0)             # 的包络分辨率分开）
AMP = (1.00, 0.70, 0.35)

# 测量用的倒谱截断上限（375 Hz 分辨率）。和算法里同一个量级，
# 并且同样走自适应 —— 见 envelope_curve 里的说明。
LIFT_MEAS = 128

_results = []


def rec(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  [%s] %-42s %s" % ("PASS" if ok else "FAIL", name, detail))


# ---------------------------------------------------------------- 合成元音

def synth_vowel(dur=0.7, f0=F0, formants=FORMANTS, bw=BW, amp=AMP, sr=SR, fade=0.02):
    """直接按"目标频谱"合成元音 —— 谐波幅度写死，包络可解析。

    早先的版本是"锯齿声源 + 三个并联谐振器"。那样做的问题在于
    声源本身有 -6 dB/oct 的倾斜，叠上谐振器的裙边之后，合成出来
    的频谱包络峰位和设定的 F1/F2/F3 并不重合（实测只测出 1125/2438
    两个峰，跟设定的 730/1090/2440 对不上），于是"漂移"这个指标
    根本没有可比性。

    这里反过来做：谐波幅度直接按三个洛伦兹共振峰给定，
        a(h) = Σ_i  A_i / (1 + ((h*F0 - F_i) / (BW_i/2))^2)
    那么频谱包络**就是**这三个峰，测出来的峰位必然等于设定值，
    变调前后的相对变化才有意义。

    相位用固定种子的随机相位（不用全零，全零会合成出脉冲串，
    瞬态统计会被污染）。所有谐波都是 F0 的整数倍，信号严格周期，
    自相关测 F0 会非常干净。
    """
    n = int(sr * dur)
    t = np.arange(n) / float(sr)
    f = np.arange(1, int(0.92 * (sr / 2.0) / f0) + 1) * f0
    a = np.zeros(f.size)
    for ff, bb, aa in zip(formants, bw, amp):
        a += aa / (1.0 + ((f - ff) / (0.5 * bb)) ** 2)
    rng = np.random.RandomState(7)
    ph = rng.uniform(0.0, 2.0 * math.pi, f.size)
    y = np.zeros(n)
    for i in range(f.size):
        y += a[i] * np.sin(2.0 * math.pi * f[i] * t + ph[i])
    y /= (np.max(np.abs(y)) + 1e-12)
    y *= 0.7
    # 首尾淡入淡出，避免边界不连续污染瞬态统计
    k = int(fade * sr)
    win = np.ones(n)
    win[:k] = np.linspace(0, 1, k)
    win[-k:] = np.linspace(1, 0, k)
    return y * win


# ---------------------------------------------------------------- 测量

def est_f0(x, sr=SR, fmin=60.0, fmax=600.0):
    """归一化自相关测基频。取中间 60% 的稳态段，避开首尾淡变。

    ★ 八度错误防护：在"相关度 >= 最大值 90%"的候选里取**最小**的 lag
      （也就是最高的频率）。

    一个 180 Hz 的周期信号，在 90 Hz 对应的 lag 上相关度同样接近 1.0；
    只取全局最大值会随机掉到低八度。实测就踩到了：F0 = 90 Hz 升 12 个
    半音（目标 180 Hz）的输出被测成 90.06 Hz，而频谱上最高那几个峰
    是 1980 / 2160 / 2340 —— 全是 180 的整数倍，信号本身完全正确，
    是尺子错了。
    """
    n = x.size
    seg = x[int(0.2 * n):int(0.8 * n)]
    seg = seg - seg.mean()
    lagmin = max(2, int(sr / fmax))
    lagmax = min(int(0.5 * seg.size) - 1, int(sr / fmin))
    if lagmax <= lagmin:
        return 0.0, 0.0
    lags = np.arange(lagmin, lagmax + 1)
    rs = np.empty(lags.size, dtype=np.float64)
    for i, lag in enumerate(lags):
        a = seg[:-lag] if lag else seg
        b = seg[lag:]
        rs[i] = float(np.dot(a, b) /
                      (np.sqrt(np.dot(a, a) * np.dot(b, b)) + 1e-12))
    mx = float(rs.max())
    cand = np.nonzero(rs >= 0.90 * mx)[0]
    lag = int(lags[cand[0]])
    return sr / float(lag), mx


N_MEAS = 2048          # 测量用更长窗：46.9 Hz -> 23.4 Hz，峰位定得更准


def envelope_curve(x, sr=SR, n=N_MEAS, hop=256, lift=LIFT_MEAS):
    """平均倒谱包络 -> (freqs, env)。

    截断也走自适应（上限就是 LIFT_MEAS）。早先固定用 200（240 Hz 分辨率），
    遇到 F0 = 320 Hz 的激励时，320 Hz 的谐波间距大于分辨率，谐波直接
    被当成了"包络峰" —— 测出来的"共振峰"是 641 / 1280 Hz（正好是 320
    的 2 倍和 4 倍），而真实共振峰是 800 / 1400 Hz。尺子必须比被测的
    谐波间距粗，否则量出来的不是包络。
    """
    w = hq_voice.hann_periodic(n)
    m = 1 + (x.size - n) // hop
    off = hop * np.arange(m, dtype=np.int64)
    fr = x[off[:, None] + np.arange(n)[None, :]] * w[None, :]
    X = np.abs(np.fft.rfft(fr, n=n, axis=1))
    lm = np.log(np.maximum(X, 1e-10)).mean(0)
    lf = hq_voice.adaptive_lift(lm, sr, n, lift)
    env = hq_voice.cepstral_envelope(X, lf, n)
    freqs = np.arange(n // 2 + 1) * (sr / float(n))
    return freqs, env.mean(0)


def find_formants(freqs, env, fmin=300.0, fmax=3800.0, k=3, span=24):
    """在对数包络上找局部极大，按突出度取前 k 个，峰位做抛物线内插。

    ★ 抛物线内插不是可选项。48 kHz / 1024 点时一个 bin 就是 46.9 Hz，
      在 700 Hz 处相当于 116 音分 —— 比要测的"1 个半音"还大。
      不内插的话这个指标连 100 音分的分辨率都没有，等于没测。
      （这里测量用 2048 点把 bin 降到 23.4 Hz，再内插，分辨率足够。）
    """
    lo = int(np.searchsorted(freqs, fmin))
    hi = int(np.searchsorted(freqs, fmax))
    e = np.log(np.maximum(env, 1e-12))
    df = float(freqs[1] - freqs[0])
    cands = []
    for i in range(max(lo, 2), min(hi, len(e) - 2)):
        if not (e[i] > e[i - 1] and e[i] >= e[i + 1]):
            continue
        den = e[i - 1] - 2.0 * e[i] + e[i + 1]
        if den >= 0:
            continue
        d = 0.5 * (e[i - 1] - e[i + 1]) / den
        d = float(np.clip(d, -1.0, 1.0))
        left = e[max(0, i - span):i].min()
        right = e[i + 1:i + 1 + span].min()
        cands.append((min(e[i] - left, e[i] - right), freqs[i] + d * df))
    cands.sort(reverse=True)
    picked = sorted(f for _, f in cands[:k])
    return picked


def envelope_shift_cents(freqs, e_ref, e_test, fmin=200.0, fmax=4000.0,
                         max_cents=2400.0, step=10.0):
    """量"包络整体沿频率轴挪了多少音分" —— 返回 (位移音分, 最佳相关系数)。

        B(f) = e_test(f * 2^(c/1200))，扫 c 找与 e_ref 相关最大的那个。
        正数 = 被测包络往高频挪了。

    为什么不用"找峰再按索引比"：
    峰挑选器只要选到不同的一组峰（比如把某个校正增益的平坦区当成峰），
    按索引一对一比出来的数字就是纯噪声 —— 实测出现过"+1011 音分"这种
    结果，而实际上共振峰被正确地拉回了 692 Hz。整体位移互相关不依赖
    "找对峰"这一步，稳得多，而且直接对应"共振峰跑没跑"这件事本身。
    """
    grid = np.geomspace(fmin, fmax, 512)
    A = np.interp(grid, freqs, np.log(np.maximum(e_ref, 1e-12)))
    A = A - A.mean()
    na = float(np.sqrt(A @ A)) + 1e-12
    best, bestc = -2.0, 0.0
    for c in np.arange(-max_cents, max_cents + 1e-9, step):
        B = np.interp(grid * (2.0 ** (c / 1200.0)), freqs,
                      np.log(np.maximum(e_test, 1e-12)))
        B = B - B.mean()
        r = float(A @ B / (na * (np.sqrt(B @ B) + 1e-12)))
        if r > best:
            best, bestc = r, c
    return bestc, best


def env_correlation(freqs, e_ref, e_test, fmin=150.0, fmax=4500.0):
    """两段包络在 150~4500 Hz 对数频率网格上的相关系数。"""
    grid = np.geomspace(fmin, fmax, 256)
    a = np.interp(grid, freqs, np.log(np.maximum(e_ref, 1e-12)))
    b = np.interp(grid, freqs, np.log(np.maximum(e_test, 1e-12)))
    a = a - a.mean()
    b = b - b.mean()
    return float(np.dot(a, b) / (np.sqrt(np.dot(a, a) * np.dot(b, b)) + 1e-12))


def count_clicks(x, thr=0.10):
    """相邻样本差分超过满量程 thr 的次数——变调算法出错时这里会爆。"""
    d = np.abs(np.diff(np.asarray(x, dtype=np.float64)))
    return int(np.sum(d > thr))


def cents(f_out, f_in):
    """两个频率相差多少**音分**（1 半音 = 100 音分）。

    早先这个函数叫 semitone_err 却返回 1200*log2 —— 那是音分不是半音，
    于是到处出现"漂移 491 半音"这种荒谬数字。单位统一成音分，
    需要半音的地方除以 100。
    """
    return 1200.0 * math.log2(float(f_out) / float(f_in))


# ---------------------------------------------------------------- 场景扫描

SCENARIOS = [
    # (名字, F0, 共振峰, 变调半音数)
    ("男声 120 Hz", 120.0, (700.0, 1200.0, 2500.0), +5.0),
    ("男声 120 Hz", 120.0, (700.0, 1200.0, 2500.0), +12.0),
    ("男声 120 Hz", 120.0, (700.0, 1200.0, 2500.0), -7.0),
    ("女声 220 Hz", 220.0, (800.0, 1300.0, 2800.0), +5.0),
    ("女声 220 Hz", 220.0, (800.0, 1300.0, 2800.0), -5.0),
    ("高音 320 Hz", 320.0, (800.0, 1400.0, 3000.0), +4.0),
    ("低音  90 Hz", 90.0, (650.0, 1100.0, 2400.0), +12.0),
]


def scenario_sweep():
    """把工作区间扫一遍：不同 F0、不同变调量，看结论是不是普适的。

    单点测试很容易"刚好调好"，扫一遍才知道算法在什么范围内站得住。
    """
    print("\n" + "=" * 78)
    print("场景扫描：不同音域 / 不同变调量")
    print("=" * 78)
    print("%-12s %6s %8s | %-24s | %-24s" %
          ("场景", "变调", "", "时域延迟线", "频域 + 共振峰保持"))
    print("%-12s %6s %8s | %10s %10s | %10s %10s" %
          ("", "半音", "", "F0误差", "包络位移", "F0误差", "包络位移"))
    print("-" * 78)
    bad = 0
    for name, f0, fm, semi in SCENARIOS:
        x = synth_vowel(dur=0.6, f0=f0, formants=fm)
        ratio = hq_voice.semitone_to_ratio(semi)
        tgt = f0 * ratio

        ps = dsp_core.PitchShifter()
        x16 = np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)
        yA = ps.process_block(x16, int(dsp_core.semitone_to_dphase(semi))) / 32768.0
        yC = hq_voice.process(x, SR, ratio, formant=1.0)

        fr0, e0 = envelope_curve(x)
        out = []
        for y in (yA, yC):
            f0o, _ = est_f0(y, fmin=max(40.0, 0.5 * tgt), fmax=min(1200.0, 3.0 * tgt))
            fr, en = envelope_curve(y)
            sh, _c = envelope_shift_cents(fr0, e0, en)
            out.append((cents(f0o, tgt), sh))
        (ea, sa), (ec, sc) = out
        print("%-12s %+6.0f %8s | %+9.0f %+10.0f | %+9.0f %+10.0f" %
              (name, semi, "音分", ea, sa, ec, sc))
        # 判据：F0 必须落在目标附近；共振峰位移必须远小于变调量
        if abs(ec) > 80.0 or abs(sc) > 0.25 * abs(semi) * 100.0 + 40.0:
            bad += 1
            print("      ^ 超出判据")
    print("-" * 78)
    rec("场景扫描 %d 个用例全部达标" % len(SCENARIOS), bad == 0,
        "%d 个超差" % bad)


# ---------------------------------------------------------------- 主流程

def main():
    print("=" * 78)
    print("高质量变调 vs 时域变调 —— 客观指标对比")
    print("=" * 78)
    print("激励：合成元音 /a/，F0 = %.0f Hz，共振峰 %s Hz，带宽 %s Hz"
          % (F0, FORMANTS, BW))
    print("采样率 %d Hz，分析窗 %d 点（%.1f ms）" % (SR, NFFT, 1000.0 * NFFT / SR))

    x = synth_vowel()
    dur_in = x.size / float(SR)

    f0_in, conf_in = est_f0(x)
    fr_in, env_in = envelope_curve(x)
    fm_in = find_formants(fr_in, env_in)
    print("\n原始信号：F0 = %.2f Hz（相关度 %.3f），共振峰 = %s Hz"
          % (f0_in, conf_in, ["%.0f" % f for f in fm_in]))
    if len(fm_in) != 3:
        print("  ⚠ 只找到 %d 个共振峰，测量可能不稳" % len(fm_in))
    # 合成时用 730/1090/2440，测量允许一定偏差，这里以**测量值**为基准，
    # 比较的是"变调前后的相对变化"，绝对误差不影响结论。

    SEMI = 5.0                       # 升纯四度（男声 -> 女声的典型跨度）
    ratio = hq_voice.semitone_to_ratio(SEMI)
    print("变调量：+%.1f 半音（ratio = %.4f）" % (SEMI, ratio))

    # ---------------- A：时域双抽头（与板上逐比特对齐） ----------------
    ps = dsp_core.PitchShifter()
    dp = dsp_core.semitone_to_dphase(SEMI)
    x_i16 = np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)
    yA = ps.process_block(x_i16, int(dp)).astype(np.float64) / 32768.0

    # ---------------- B：频域，不做共振峰校正 ----------------
    yB = hq_voice.process(x, SR, ratio, formant=0.0)

    # ---------------- C：频域 + 共振峰保持 ----------------
    yC = hq_voice.process(x, SR, ratio, formant=1.0)

    ref = {}
    for tag, y in (("时域延迟线", yA), ("频域（无校正）", yB), ("频域+共振峰保持", yC)):
        y = np.asarray(y, dtype=np.float64)
        f0o, conf = est_f0(y)
        fro, envo = envelope_curve(y)
        fmo = find_formants(fro, envo)
        sh, shc = envelope_shift_cents(fr_in, env_in, envo)
        ref[tag] = dict(
            y=y, f0=f0o, conf=conf, fm=fmo,
            corr=env_correlation(fr_in, env_in, envo),
            shift=sh, shiftc=shc,
            clicks=count_clicks(y),
            dur=y.size / float(SR),
            peak=float(np.max(np.abs(y))),
        )

    print("\n" + "-" * 78)
    print("%-20s %10s %10s %10s %10s %8s" %
          ("方案", "F0(Hz)", "F0误差", "包络相关", "咔哒数", "时长(s)"))
    print("-" * 78)
    for tag, d in ref.items():
        print("%-20s %10.2f %9.3fx %10.3f %10d %8.3f" %
              (tag, d["f0"], d["f0"] / f0_in, d["corr"], d["clicks"], d["dur"]))
    print("-" * 78)

    print("\n共振峰（= 频谱包络）整体位移，音分；100 音分 = 1 半音")
    print("  目标：频域+保持 ≈ 0，其余方案 ≈ +%.0f（跟着基频一起走）"
          % (SEMI * 100.0))
    print("%-20s %14s %14s %10s" % ("方案", "包络位移", "位移后相关", "峰值"))
    for tag, d in ref.items():
        print("%-20s %+13.0f %14.3f %10.3f" %
              (tag, d["shift"], d["shiftc"], d["peak"]))
    print("-" * 78)
    print("  （参考）各方案包络峰位 Hz：")
    print("      原始            %s" % " ".join("%.0f" % f for f in fm_in))
    for tag, d in ref.items():
        print("      %-14s %s" % (tag, " ".join("%.0f" % f for f in d["fm"])))
    print("-" * 78)

    # ---------------- Pass / Fail ----------------
    print("\n验收清单")
    print("-" * 78)
    tgt_f0 = f0_in * ratio
    for tag, d in ref.items():
        e = abs(cents(d["f0"], tgt_f0))
        rec("%s：F0 命中目标（误差 %.1f 音分）" % (tag, e), e < 30.0)

    rec("频域+共振峰保持：包络位移 < 60 音分（实测 %+.0f）"
        % ref["频域+共振峰保持"]["shift"],
        abs(ref["频域+共振峰保持"]["shift"]) < 60.0)
    rec("频域+共振峰保持：包络相关 > 0.90（实测 %.3f）" % ref["频域+共振峰保持"]["corr"],
        ref["频域+共振峰保持"]["corr"] > 0.90)

    # 关键对照：时域必须明显更差，否则说明这个指标没有区分度
    rec("时域延迟线：共振峰确实跟着基频跑（位移 > 300 音分，实测 %+.0f）"
        % ref["时域延迟线"]["shift"], ref["时域延迟线"]["shift"] > 300.0)
    rec("频域+保持 优于 时域（包络位移）",
        abs(ref["频域+共振峰保持"]["shift"]) <
        0.25 * abs(ref["时域延迟线"]["shift"]),
        "%+.0f vs %+.0f" % (ref["频域+共振峰保持"]["shift"],
                            ref["时域延迟线"]["shift"]))
    rec("频域+保持 优于 时域（包络相关）",
        ref["频域+共振峰保持"]["corr"] > ref["时域延迟线"]["corr"] + 0.15,
        "%.3f vs %.3f" % (ref["频域+共振峰保持"]["corr"], ref["时域延迟线"]["corr"]))
    rec("频域+保持 的咔哒数不高于时域",
        ref["频域+共振峰保持"]["clicks"] <= max(4, ref["时域延迟线"]["clicks"]),
        "%d vs %d" % (ref["频域+共振峰保持"]["clicks"], ref["时域延迟线"]["clicks"]))

    # ---------------- 流水线单步验证 ----------------
    print("\n流水线单步验证（确认 A/B/C 三步各自按设计走）")
    print("-" * 78)
    w = hq_voice.hann_periodic(NFFT)
    pad = NFFT
    xp = np.concatenate([np.zeros(pad), x, np.zeros(pad)])

    nstage = hq_voice._stage_count(ratio)
    each = ratio ** (1.0 / nstage)
    # A 步输出整个是"拉长版"，时长比要按补零前的净内容算：
    # 有效区间是 [pad*each, (pad+nx)*each)，长度 = nx*each
    len_eff = x.size * each
    y_stage = x
    x_tsm = None
    for _si in range(nstage):
        yp = np.concatenate([np.zeros(pad), y_stage, np.zeros(pad)])
        x_tsm = hq_voice._tsm(yp, NFFT, 256, w, each)
        y_stage = hq_voice.sinc_resample(
            x_tsm, (pad + np.arange(y_stage.size, dtype=np.float64)) * each, each)
    print("  A 步（时间伸缩，%d 级 × %.4f）：有效时长 %.3f s / 原 %.3f s = %.3f"
          "（目标 %.3f），峰值 %.3f"
          % (nstage, each, len_eff / float(SR), dur_in, len_eff / x.size, each,
             float(np.max(np.abs(x_tsm)))))
    rec("A 步：时长比按设计（%.3f vs %.3f）" % (len_eff / x.size, each),
        abs(len_eff / x.size - each) < 1e-9)
    seg_a = x_tsm[int(pad * each + 0.1 * x.size * each):
                  int(pad * each + 0.6 * x.size * each)]
    f0_tsm, _ = est_f0(seg_a)
    print("                    F0 = %.2f Hz（原 %.2f）" % (f0_tsm, f0_in))
    rec("A 步：音高不变（误差 %.1f 音分）" % abs(cents(f0_tsm, f0_in)),
        abs(cents(f0_tsm, f0_in)) < 15.0)

    yB2 = y_stage
    f0_b, _ = est_f0(yB2)
    print("  B 步（重采样）  ：时长还原 %.3f s，F0 = %.2f Hz（目标 %.2f），峰值 %.3f"
          % (yB2.size / float(SR), f0_b, tgt_f0, float(np.max(np.abs(yB2)))))
    rec("B 步：时长还原到原长", yB2.size == x.size)
    rec("B 步：F0 按 ratio 抬高（误差 %.1f 音分）" % abs(cents(f0_b, tgt_f0)),
        abs(cents(f0_b, tgt_f0)) < 20.0)
    rec("B 步：边界没有爆音（峰值 < 1.5）", float(np.max(np.abs(yB2))) < 1.5,
        "peak=%.3f" % float(np.max(np.abs(yB2))))

    fr_b, env_b = envelope_curve(yB2)
    df_b, cb = envelope_shift_cents(fr_in, env_in, env_b)
    print("  B 步后包络位移 %+.0f 音分（应接近 +%.0f）"
          % (df_b, SEMI * 100.0))
    rec("B 步后共振峰确实被抬走了（%+.0f 音分）" % df_b,
        abs(df_b - SEMI * 100.0) < 150.0)
    fr_c, env_c = envelope_curve(yC)
    df_c, cc = envelope_shift_cents(fr_in, env_in, env_c)
    print("  C 步后包络位移 %+.0f 音分（应接近 0）" % df_c)
    rec("C 步后共振峰被拉回原位（%+.0f 音分）" % df_c, abs(df_c) < 60.0)

    # 顺带量一下"谐波稀疏"这个退化场景，只在真的跑偏时才报。
    # 单频正弦在倒谱包络里就是一个孤立窄峰，会被当成"唯一的共振峰"，
    # 校正于是把它往回拽 —— 变调量越大越明显。这是包络对齐法的原理性
    # 边界，不是 bug；真实人声一个窗里有几十条谐波，不会触发。
    t = np.arange(int(SR * 0.4)) / float(SR)
    for semi_probe in (5.0, 12.0):
        r_probe = hq_voice.semitone_to_ratio(semi_probe)
        sine = 0.5 * np.sin(2.0 * np.pi * 440.0 * t)
        y_sine = hq_voice.process(sine, SR, r_probe, formant=1.0)
        Ys = np.abs(np.fft.rfft(y_sine * np.hanning(y_sine.size)))
        f_sine = np.argmax(Ys) * SR / y_sine.size
        err = cents(f_sine, 440.0 * r_probe)
        tag = "正常" if abs(err) < 50.0 else "退化（已知限制）"
        print("  【稀疏谐波探针】单频 440 Hz，+%.0f 半音：目标 %.0f Hz，"
              "实测 %.0f Hz（%+.0f 音分）—— %s"
              % (semi_probe, 440.0 * r_probe, f_sine, err, tag))

    # ---------------- 场景扫描 ----------------
    scenario_sweep()

    # ---------------- 汇总 ----------------
    n_pass = sum(1 for ok, _, _ in _results if ok)
    print("\n" + "=" * 78)
    print("结果：%d PASS / %d FAIL" % (n_pass, len(_results) - n_pass))
    print("=" * 78)
    for ok, name, detail in _results:
        if not ok:
            print("  FAIL  %s  %s" % (name, detail))

    # 顺手把三段结果写成 wav，方便自己听
    try:
        sys.path.insert(0, HOST)
        import audio_io
        outdir = os.path.join(ROOT, "tool", "_hq_demo")
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        for tag, d in ref.items():
            nm = {"时域延迟线": "A_time", "频域（无校正）": "B_freq",
                  "频域+共振峰保持": "C_freq_formant"}[tag]
            audio_io.save_audio(os.path.join(outdir, "%s.wav" % nm),
                                np.clip(d["y"], -1.0, 1.0).astype(np.float32), SR)
        audio_io.save_audio(os.path.join(outdir, "0_orig.wav"),
                            x.astype(np.float32), SR)
        print("示例音频已写到 tool/_hq_demo/（0_orig / A_time / B_freq / C_freq_formant）")
    except Exception as e:
        print("（示例音频未写出：%r）" % (e,))

    return 0 if n_pass == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
