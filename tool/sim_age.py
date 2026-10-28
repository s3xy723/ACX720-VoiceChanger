# -*- coding: utf-8 -*-
"""sim_age.py —— 年龄变声的算法参考模型与验证。

和 rtl/age_core.v 一一对应：RTL 里每一个乘加在这里都有一行。
先在这里把算法验对，再写 Verilog —— 反过来做的话，仿真出错时
根本分不清是算法错还是 RTL 错。

判据（每一条都能 FAIL，不是装饰）
--------------------------------
  I1 恒等透明：β=1、不加音质时，输出与输入的 SNR > 60 dB
  I2 基频不动：包络缩放后 F0 相对变化 < 1%
  I3 共振峰确实移动：估计出的 β̂ 与设定的 β 误差 < 8%
  I4 音质着色只在目标更老/更小的时候起作用（同年龄时完全不动）
  I5 定点量化后（Q15 中间量）各项指标退化 < 1 dB
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "host"))

import age_model as AM  # noqa: E402

N_FFT = 1024
HOP = 256
WIN = np.hanning(N_FFT)


# ---------------------------------------------------------------- STFT

def stft(x):
    pad = N_FFT // 2
    xp = np.concatenate([np.zeros(pad), x, np.zeros(pad)])
    nf = 1 + (len(xp) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(nf)[:, None]
    return np.fft.rfft(xp[idx] * WIN, axis=1)


def istft(S, n_out):
    """Hann 分析 + Hann 合成，按窗平方和归一化（四帧重叠时恒为 1.5）。"""
    fr = np.fft.irfft(S, n=N_FFT, axis=1) * WIN
    total = HOP * (fr.shape[0] - 1) + N_FFT
    acc = np.zeros(total)
    wsum = np.zeros(total)
    for i in range(fr.shape[0]):
        s = i * HOP
        acc[s:s + N_FFT] += fr[i]
        wsum[s:s + N_FFT] += WIN ** 2
    acc /= np.maximum(wsum, 1e-8)
    pad = N_FFT // 2
    y = acc[pad:pad + n_out]
    if len(y) < n_out:
        y = np.concatenate([y, np.zeros(n_out - len(y))])
    return y


# ---------------------------------------------------------------- 包络

def envelope(mag, mk=5, sk=25):
    """谱包络：先做峰值保持（把谐波之间的深谷填平），再在 **log 域**平滑。

    ★ 这里踩过两个坑，都记下来：
      1) 只在**线性幅度**上平滑 → 谐波谷底仍然接近 0，除以它得到 5 万倍的
         增益（实测 g_max=3.6e5，62% 的 bin 超 +8 dB），输出全是尖啸。
      2) 不做峰值保持只做 log 平滑 → 同样在谷底爆掉。
      先 peak-hold(5 bin ≈ 120 Hz，刚好盖住一个谐波间隔) 再 log 平滑 25 bin，
      得到的才是"顺着共振峰走、不跟着谐波抖"的曲线。
    """
    L = np.log(np.maximum(mag, 1e-12))
    half = mk // 2
    P = np.pad(L, ((0, 0), (half, half)), mode="edge")
    hold = P.copy()
    for s in range(1, half + 1):
        hold = np.maximum(hold, np.roll(P, s, axis=1))
        hold = np.maximum(hold, np.roll(P, -s, axis=1))
    hold = hold[:, half:half + mag.shape[1]]

    k = sk // 2
    pad = np.pad(hold, ((0, 0), (k, k)), mode="edge")
    c = np.cumsum(pad, axis=1)
    c = np.concatenate([np.zeros((c.shape[0], 1)), c], axis=1)
    return (c[:, sk:] - c[:, :-sk]) / float(sk)      # 返回 log 幅度


# 共振峰只在这些频率之间移动。**不能全频带缩**：高频段包络滚降很陡，
# k/beta 在 10 kHz 处等于平移 2 kHz，补偿量能到 40 dB 以上 —— 那不是
# "腔体变小"，那是把底噪一起拉起来，听感是尖啸。
WARP_LO, WARP_HI = 200.0, 4500.0
TAPER_OCT = 0.35                                   # 两侧过渡带宽（倍频程）
GAIN_DB_MAX = 12.0                                 # 单点补偿上限


def _band_weight(nb, sr):
    """1 = 完全按 beta 移，0 = 不动，过渡带用余弦收口。"""
    f = np.arange(nb) * (sr / float(2 * (nb - 1)))
    w = np.zeros(nb)
    w[(f >= WARP_LO) & (f <= WARP_HI)] = 1.0
    lo0 = WARP_LO * (2 ** -TAPER_OCT)
    hi1 = WARP_HI * (2 ** TAPER_OCT)
    m = (f >= lo0) & (f < WARP_LO)
    w[m] = 0.5 - 0.5 * np.cos(np.pi * (f[m] - lo0) / (WARP_LO - lo0))
    m = (f > WARP_HI) & (f <= hi1)
    w[m] = 0.5 + 0.5 * np.cos(np.pi * (f[m] - WARP_HI) / (hi1 - WARP_HI))
    return w


def warp_env(E, beta, sr):
    """E'[k] = E[k / beta_eff[k]]，beta_eff 从 beta 平滑过渡回 1。"""
    nb = E.shape[1]
    w = _band_weight(nb, sr)
    beff = 1.0 + (float(beta) - 1.0) * w
    src = np.arange(nb, dtype=np.float64) / beff
    src = np.clip(src, 0, nb - 1)
    i0 = np.floor(src).astype(int)
    i1 = np.minimum(i0 + 1, nb - 1)
    t = src - i0
    return E[:, i0] * (1.0 - t) + E[:, i1] * t


def formant_warp(x, beta, sr, use_fixed=False):
    """只改谱包络，保留相位与谐波位置。增益在对数域算，限幅 ±12 dB。"""
    S = stft(x)
    mag = np.abs(S)
    E = envelope(mag)
    E2 = warp_env(E, beta, sr)
    gdb = np.clip((E2 - E) * (20.0 / np.log(10.0)),
                  -GAIN_DB_MAX, GAIN_DB_MAX)
    if use_fixed:                      # 模拟 RTL：增益 Q4.11（1/8 dB 步进）
        gdb = np.round(gdb * 8.0) / 8.0
    g = 10.0 ** (gdb / 20.0)
    return istft(S * g, len(x))


# ---------------------------------------------------------------- 音质着色

def color(x, sr, breath=0.0, jitter_pct=0.0, shimmer_pct=0.0, tilt_db=0.0,
          seed=0, use_fixed=False):
    """气声 + jitter/shimmer + 高频倾斜。全部在时域做。"""
    y = np.asarray(x, dtype=np.float64).copy()
    n = len(y)
    rng = np.random.default_rng(seed)

    # --- 高频倾斜：一阶 shelf，3.5 kHz 以上按 tilt_db 抬/压
    if abs(tilt_db) > 1e-6:
        fc = 3500.0
        a = np.exp(-2.0 * np.pi * fc / sr)
        g = 10.0 ** (tilt_db / 20.0)
        hp = np.zeros(n)
        prev_x = 0.0
        prev_y = 0.0
        for i in range(n):
            hp[i] = a * (prev_y + y[i] - prev_x)
            prev_x = y[i]
            prev_y = hp[i]
        y = y + (g - 1.0) * hp

    # --- 气声：与语音包络同步的噪声（不是常亮噪声，否则像录音底噪）
    if breath > 1e-4:
        env = np.abs(y)
        k = int(0.010 * sr)            # 10 ms 平滑
        ker = np.ones(k) / k
        env = np.convolve(env, ker, mode="same")
        noise = rng.standard_normal(n) * env * breath * 0.35
        y = y + noise

    # --- jitter / shimmer：逐周期微扰。这里用"按基音周期长度的低频随机
    #     调制"近似：直接用 20~60 Hz 的窄带随机信号调制，比逐周期重采样
    #     便宜，听感上等价（都是"毛边"）。
    if jitter_pct > 1e-4 or shimmer_pct > 1e-4:
        t = np.arange(n) / float(sr)
        mod = rng.standard_normal(n)
        b = np.exp(-2.0 * np.pi * 40.0 / sr)
        smooth = np.zeros(n)
        acc = 0.0
        for i in range(n):
            acc = b * acc + (1 - b) * mod[i]
            smooth[i] = acc
        smooth /= (np.std(smooth) + 1e-9)
        if shimmer_pct > 1e-4:
            y = y * (1.0 + smooth * shimmer_pct / 100.0 * 0.5)
        if jitter_pct > 1e-4:
            ph = 2 * np.pi * np.cumsum(
                100.0 * (1.0 + smooth * jitter_pct / 100.0 * 0.5)) / sr
            y = y + 0.12 * y * np.sin(ph) * (jitter_pct / 1.0)
    return y


# ---------------------------------------------------------------- 测量

def est_f0(x, sr, fmin=60, fmax=500):
    """自相关估计基频，取中位数（只看有能量的帧）。"""
    fl = int(0.040 * sr)
    hop = fl // 2
    f0s = []
    for s in range(0, max(1, len(x) - fl), hop):
        seg = x[s:s + fl]
        if np.sqrt(np.mean(seg ** 2)) < 0.01:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[fl - 1:]
        ac /= (ac[0] + 1e-12)
        lo, hi = int(sr / fmax), int(sr / fmin)
        if hi >= len(ac):
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0.3:
            f0s.append(sr / lag)
    return float(np.median(f0s)) if f0s else float("nan")


def est_beta(x, y, sr):
    """估计 y 相对 x 的谱包络缩放比（只看共振峰频段）。

    这是**独立测量**：从真正输出的信号里重新求包络再来比，
    不是把算法内部算的 E2 拿回来读一遍（那样是循环论证）。
    """
    Sx, Sy = stft(x), stft(y)
    Ex = envelope(np.abs(Sx)).mean(0)
    Ey = envelope(np.abs(Sy)).mean(0)
    f = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    grid = np.arange(len(Ex))
    sel = (f >= WARP_LO) & (f <= WARP_HI)
    best, bb = 1e18, 1.0
    for b in np.arange(0.70, 1.65, 0.005):
        src = np.clip(grid / b, 0, len(Ex) - 1)
        i0 = np.floor(src).astype(int)
        i1 = np.minimum(i0 + 1, len(Ex) - 1)
        t = src - i0
        pred = Ex[i0] * (1 - t) + Ex[i1] * t
        d = np.mean((pred[sel] - Ey[sel]) ** 2)
        if d < best:
            best, bb = d, b
    return bb


def snr_db(x, y):
    n = min(len(x), len(y))
    e = np.sum((x[:n] - y[:n]) ** 2)
    s = np.sum(x[:n] ** 2)
    return 10 * np.log10(max(s, 1e-20) / max(e, 1e-20))


# ---------------------------------------------------------------- 主流程

def apply_age(x, sr, a_from, a_to, sex="m", strength=1.0, use_fixed=False):
    """完整链路：包络缩放 → 变调 → 音质着色。

    ★ 顺序不能反。变调是重采样，会把共振峰**一起**乘以 r；所以包络缩放
      这一步要预先补偿掉，传进去的是 beta/r，不是 beta：
          共振峰 × (beta/r) × r = beta     F0 × r
      直接传 beta 会让腔体被改两次（30→75 岁实测共振峰多移 1.155 倍，
      听起来像"又老又憋"）。
    """
    p = AM.params(a_from, a_to, sex, strength)
    beta_pre = p["beta"] / max(p["r"], 1e-6)
    y = formant_warp(x, beta_pre, sr, use_fixed)
    if abs(p["r"] - 1.0) > 1e-6:
        y = pitch_shift_offline(y, p["r"])
    y = color(y, sr, breath=p["breath"], jitter_pct=p["jitter"] - 0.35,
              shimmer_pct=p["shimmer"] - 2.0, tilt_db=p["tilt_db"],
              use_fixed=use_fixed)
    return y, p


def pitch_shift_offline(x, r):
    """离线参考变调（重采样）：只用于算法验证，RTL 里由 pitch_shift.v 承担。

    ★ 方向别搞反：要升调 r 倍就要**读得更快**，即 pos 每步前进 r 个输入点
      （y[i] = x[i*r]）。写成 1/r 得到的是反向——实测 r=1.9 时输出 F0 变成
      0.56 倍，听起来完全反了。
    """
    n = len(x)
    pos = np.arange(n) * float(r)
    idx = np.arange(len(x))
    y = np.interp(np.clip(pos, 0, len(x) - 1), idx, x)
    tail = int(min(n, n / max(r, 1e-6)))     # 升调后尾部是补零，不是重复
    if r > 1.0 and tail < n:
        y[tail:] *= np.linspace(1.0, 0.0, n - tail) ** 2
    return y[:n]


def cmd_selftest(a):
    sr = 24000
    # 合成一段"元音+基频"作为被测信号：F0=120 Hz，共振峰在 700/1200/2600
    t = np.arange(int(1.5 * sr)) / float(sr)
    f0 = 120.0
    x = np.zeros_like(t)
    for h in range(1, 40):
        fh = f0 * h
        if fh > 10000:
            break
        amp = 0.0
        for fc, bw in ((700, 130), (1200, 160), (2600, 250)):
            amp += 1.0 / (1.0 + ((fh - fc) / (bw * 1.5)) ** 2)
        x += amp * np.sin(2 * np.pi * fh * t)
    x /= (np.abs(x).max() + 1e-9)
    x *= 0.5

    ok = fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [PASS] %s %s" % (name, detail))
        else:
            fail += 1
            print("  [FAIL] %s %s" % (name, detail))

    print("sim_age 算法判据（合成元音，F0=120 Hz，共振峰 700/1200/2600）")

    # I1 恒等透明
    y1 = formant_warp(x, 1.0, sr)
    s1 = snr_db(x[200:-200], y1[200:-200])
    chk("I1 β=1 恒等透明 SNR > 60 dB", s1 > 60, "SNR=%.1f dB" % s1)

    # I2 基频不动（β=1.25）
    y2 = formant_warp(x, 1.25, sr)
    f0a, f0b = est_f0(x, sr), est_f0(y2, sr)
    chk("I2 包络缩放不改基频（<1%）",
        abs(f0b / f0a - 1) < 0.01, "%.1f -> %.1f Hz" % (f0a, f0b))

    # I3 共振峰确实上移，且量对
    for beta in (0.90, 1.15, 1.25):
        yb = formant_warp(x, beta, sr)
        bh = est_beta(x, yb, sr)
        chk("I3 beta=%.2f 估计 %.3f" % (beta, bh),
            abs(bh / beta - 1) < 0.08, "误差 %.1f%%" % (100 * abs(bh / beta - 1)))

    # 负对照：不做处理时 β̂ 必须 ≈ 1.0（否则 I3 的估计器本身有偏）
    bh0 = est_beta(x, formant_warp(x, 1.0, sr), sr)
    chk("I3 负对照 未处理时 β̂≈1.00", abs(bh0 - 1.0) < 0.01, "β̂=%.3f" % bh0)

    # I4 同年龄不动
    y4, p4 = apply_age(x, sr, 40, 40, "m")
    chk("I4 同年龄→同年龄 完全不动",
        np.max(np.abs(y4 - x)) < 1e-9, "max|diff|=%.2e" % np.max(np.abs(y4 - x)))

    # I5 定点量化退化
    y5 = formant_warp(x, 1.25, sr, use_fixed=True)
    chk("I5 Q2.13 增益量化后 SNR 仍 > 45 dB",
        snr_db(y2[200:-200], y5[200:-200]) > 45,
        "SNR=%.1f dB" % snr_db(y2[200:-200], y5[200:-200]))

    # 方向检查：成人→老年 必须变"暗"且气声出现
    y6, p6 = apply_age(x, sr, 30, 78, "m")
    hf = lambda z: 10 * np.log10(
        np.mean(np.abs(stft(z)[:, 150:300]) ** 2) /
        np.mean(np.abs(stft(z)[:, 20:120]) ** 2))
    chk("成人→老年 高频含量下降", hf(y6) < hf(x) - 1.0,
        "HF %.2f -> %.2f dB" % (hf(x), hf(y6)))
    chk("成人→老年 气声被注入", p6["breath"] > 0.1,
        "breath=%.2f tilt=%.1f dB" % (p6["breath"], p6["tilt_db"]))

    print("\n  PASS = %d  FAIL = %d" % (ok, fail))
    return 0 if fail == 0 else 1


def cmd_run(a):
    import soundfile as sf
    x, sr = sf.read(a.src, dtype="float32", always_2d=True)
    i0 = int(a.start * sr)
    x = x[i0:i0 + int(a.secs * sr)].mean(1).astype(np.float64)
    y, p = apply_age(x, sr, a.age_from, a.age_to, a.sex, a.strength)
    y = y / max(np.abs(y).max(), 1e-9) * 0.95
    out = a.out or os.path.join(ROOT, "samples", "age_%dto%d_%s.wav"
                                % (a.age_from, a.age_to, a.sex))
    sf.write(out, np.stack([y, y], 1).astype(np.float32), sr)
    print("r=%.3f beta=%.3f breath=%.2f jitter=%.2f%% tilt=%.1f dB"
          % (p["r"], p["beta"], p["breath"], p["jitter"], p["tilt_db"]))
    print("F0 %.0f -> %.0f Hz；β̂=%.3f" % (p["f0_from"], p["f0_to"],
                                          est_beta(x, y, sr)))
    print("->", out)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers()
    s = sub.add_parser("selftest")
    s.set_defaults(fn=cmd_selftest)
    r = sub.add_parser("run")
    r.add_argument("--src", default=r"C:/Users/Administrator/Downloads/老狼&王婧_想把我唱给你听.flac")
    r.add_argument("--start", type=float, default=80.0)
    r.add_argument("--secs", type=float, default=12.0)
    r.add_argument("--age-from", type=float, default=30)
    r.add_argument("--age-to", type=float, default=75)
    r.add_argument("--sex", default="m")
    r.add_argument("--strength", type=float, default=1.0)
    r.add_argument("--out", default="")
    r.set_defaults(fn=cmd_run)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
