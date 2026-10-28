# -*- coding: utf-8 -*-
"""
hq_voice.py —— 高质量变调：相位声码器 + 共振峰保持

这个模块解决什么问题
--------------------
板上（rtl/pitch_shift.v）和 dsp_core.PitchShifter 用的是**时域**双抽头
Hann 交叉淡化延迟线。它在时域里以变化的速度读一条延迟线，所以频谱上的
每一个峰——基频、各次谐波、共振峰——全都被同一个比例 r 一起缩放。

问题就出在这里：

    人耳判断"音有多高"靠基频，判断"这是谁"靠共振峰（声道形状）。
    升一个八度时共振峰也跟着翻倍，等于把说话人的声道变短了一半。

这就是"花栗鼠效应"：听起来不是"换了一个人用更高的声音说话"，
而是"一个小孩在说话"。动漫变声、男声变女声要的是前者，时域变调
只能给你后者。这不是参数没调好，是原理上就分不开这两件事。

做法：把"基频"和"共振峰"拆成两条路
----------------------------------
    x ──A──▶ 相位声码器时间伸缩 α = r ──▶ 时长 ×r，F0 不变，共振峰不变
      ──B──▶ 高质量重采样 ×1/r           ──▶ 时长还原，F0 ×r，共振峰 ×r
      ──C──▶ 共振峰包络校正              ──▶ F0 ×r，共振峰回到原处

C 步的原理：用倒谱包络分别估计原信号 x 和变调结果 y 的频谱包络 E_x / E_y，
逐帧乘一个校正增益 G = E_x / E_y。这是个随时间缓慢变化的 EQ，
只改幅度不改相位，不会引入相位失真，也不会动基频。

为什么不"直接把幅度谱沿频率轴拉伸"
----------------------------------
那是最直观的做法，但相位必须同时按新频率重新推算；否则相邻帧之间
相位对不上，合成出来是一片"沙沙"的相位噪声，比花栗鼠更难听。
相位声码器（Laroche-Dolson 1999）就是解决这件事的标准方法：
它追踪每个 bin 的**瞬时频率**，按瞬时频率而不是标称频率去推算相位。

关于分步走
----------
A+B 两步合起来就是"变调"，但中间产物是有意义的：
A 单独用是时间伸缩（变速不变调），B 单独用是变速变调（磁带快放）。
分开实现的好处是每一步都能单独验证：

    A 之后：时长 = r × 原长，F0 不变
    B 之后：时长 = 原长，     F0 × r
    C 之后：时长 = 原长，     F0 × r，共振峰不变

tool/verify_pitch_hq.py 就是按这三条逐条量。

已知限制（不藏）
----------------
- 需要 NFFT = 1024 点（48 kHz 下 21 ms）的分析窗，**不能做实时**。
  实时链路仍然走时域延迟线（延迟 < 1 ms）；本模块只用于文件处理和
  本地 AI 模式。
- 相位声码器会把瞬态（打击、爆破音）抹开一点。人声连续段影响很小，
  强打击乐旁会略微发闷。
- 共振峰校正是"包络对齐"，前提是倒谱截断选得对。默认 2.7 ms
  （48 kHz 下 128 点），对应约 370 Hz 的包络分辨率，刚好能分开
  F1 / F2 / F3。取值太小会把包络做得过于平滑、男女声听起来趋同；
  取值太大则会把谐波结构误当成包络，校正失效。

依赖：只有 numpy。
"""

import math

import numpy as np

# ---------------------------------------------------------------- 常量

NFFT = 1024          # 与 rtl/fft1024.v 同点数，将来搬上板子可以直接复用
HOP = 256            # 75% 重叠
LIFT_MS = 2.7        # 倒谱截断（毫秒）
RESAMP_TAPS = 48     # 重采样核单侧 taps（ratio>1 时按 ratio 展宽）
KAISER_BETA = 8.6    # -90 dB 旁瓣
CHUNK_FRAMES = 2048  # STFT 分块帧数，避免中间数组吃掉几百 MB
CHUNK_ENV = 512      # 共振峰校正的分块：自适应截断按这个粒度重算（2.7 秒）
GMAX = 16.0          # 包络校正增益上下限（±24 dB）

# GMAX 是实测扫出来的，不是拍的。合成元音上（F1/F2/F3 = 700/1200/2500 Hz，
# 带宽 100/140/200 Hz，升 5 个半音）放宽上限对包络相关的改善：
#     12 dB -> 0.833     24 dB -> 0.940     30 dB -> 0.946     不限制 -> 0.948
# 24 dB 之后基本平了（剩下的差距来自 375 Hz 的包络分辨率，不是增益不够）。
# 再往上放只会把低能量频段的噪声底一起抬起来（+36 dB 的补偿在真实录音上
# 就是可闻的底噪），所以定在 24 dB。
TAU_ENV = 0.03       # 包络的帧间平滑时间常数（秒）
MAX_STAGE_RATIO = 1.5   # 单级变调比上限，超了就级联（理由见 process）

_WIN = {}


def hann_periodic(n):
    """周期 Hann 窗。

    STFT 要用周期形式（首尾不重复）：对称 Hann 的首尾都是 0，
    重叠相加时会在帧边界处少算一个样本，长信号上积累成周期性的
    幅度缺口。
    """
    w = _WIN.get(n)
    if w is None:
        w = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n, dtype=np.float64) / n)
        _WIN[n] = w
    return w


# ---------------------------------------------------------------- 小工具

def _wrap_pi(a):
    """把相位差折到 (-pi, pi]。"""
    return a - 2.0 * np.pi * np.floor((a + np.pi) / (2.0 * np.pi))


def _kaiser(a, beta=KAISER_BETA):
    """Kaiser 窗 w(a) = I0(beta*sqrt(1-a^2))/I0(beta)，|a| > 1 取 0。"""
    a = np.abs(a)
    out = np.zeros(a.shape, dtype=np.float64)
    m = a <= 1.0
    if np.any(m):
        am = a[m]
        out[m] = np.i0(beta * np.sqrt(np.maximum(1.0 - am * am, 0.0))) / np.i0(beta)
    return out


def semitone_to_ratio(semi):
    return 2.0 ** (float(semi) / 12.0)


# ---------------------------------------------------------------- 倒谱包络

def cepstral_envelope(mag, lift, n):
    """倒谱包络估计。

    mag : (M, n//2+1) 非负幅度谱
    lift: 倒谱截断长度（采样点）。保留前 lift 个倒谱系数 =>
          包络能分辨的频率尺度约 sr / lift。
          48 kHz、lift = 128 -> 375 Hz，刚好分开 F1/F2/F3。

    流程：log|X| --irfft--> 实倒谱 --> 只留低倒谱（两端）--> rfft --> exp

    为什么要走倒谱而不是直接做平滑：倒谱域里"包络"和"谐波细结构"
    是分开的（低倒谱 = 缓慢变化的包络，高倒谱 = 等间距的谐波梳），
    截断就是把谐波梳整段切掉，剩下的才是声道形状。直接对幅度谱做
    移动平均的话，谐波间距随基频变化，平均窗宽就没法定。
    """
    nb = mag.shape[1]
    lm = np.log(np.maximum(mag, 1e-10))
    c = np.fft.irfft(lm, n=n, axis=1)          # (M, n) 实倒谱
    if lift < n // 2:
        c[:, lift + 1: n - lift] = 0.0          # 只留两端 = 低倒谱
    env = np.fft.rfft(c, n=n, axis=1).real      # 平滑后的 log 幅度
    return np.exp(env)[:, :nb]


# ---------------------------------------------------------------- 归一化 OLA

def adaptive_lift(lm_mean, sr, n, lift):
    """按谐波梳在倒谱上的位置，自适应收紧截断长度。

    倒谱上 quefrency = sr/F0 处会出现一个由谐波梳造成的峰。截断 lift
    必须落在这个峰**之前**，否则谐波会被当成包络的一部分 —— 那时"保持
    包络"就变成了"保持谐波不动"，等于把变调又抵消掉。

    固定截断在 F0 高的时候一定会踩进去：sr/F0 = 48000/400 = 120，
    比默认的 128 还小。实测（合成元音、只改 F0）：
        F0 = 120 Hz -> 谐波峰在 quefrency 400
        F0 = 250 Hz -> 192
        F0 = 400 Hz -> 120   ← 已经小于默认截断

    ★ 不能直接在整条倒谱上取最大值：低倒谱（包络主体）的幅度远大于
      谐波峰，F0 = 120 时会被误判成 quefrency 6（对应 8 kHz，荒谬）。
      所以只在 F0 ∈ [70, 500] Hz 对应的 quefrency 区间里找峰。

    没有明显谐波峰时（清音、纯噪声）维持默认截断，不做收紧。
    """
    c = np.abs(np.fft.irfft(lm_mean, n=n))
    q_lo = max(8, int(sr / 500.0))            # F0 上限 500 Hz
    q_hi = min(n // 2 - 1, int(sr / 70.0))    # F0 下限 70 Hz
    if q_hi <= q_lo + 4:
        return lift
    seg = c[q_lo:q_hi + 1]
    if seg.size < 4:
        return lift
    q = int(np.argmax(seg)) + q_lo
    if c[q] < 1.5 * float(np.median(seg)):
        return lift
    return int(max(24, min(lift, int(0.8 * q))))


class _OLA(object):
    """归一化重叠相加。

    不是把"窗平方之和"当成常数 1.5 直接除：时间伸缩之后合成跳距不再是
    HOP，Hann² 的叠加和不再恒定，硬除会引入周期性的音量起伏。
    这里把每一帧窗的平方累加进 nrm，最后 y = acc / nrm，
    任意跳距下幅度都是准的。
    """

    def __init__(self, n, win, length):
        self.n = n
        self.win = win
        self.acc = np.zeros(int(length) + 1, dtype=np.float64)
        self.nrm = np.zeros(int(length) + 1, dtype=np.float64)

    def add(self, frame, pos):
        p = int(pos)
        w = self.win
        self.acc[p:p + self.n] += frame * w
        self.nrm[p:p + self.n] += w * w

    def result(self, length=None):
        L = self.acc.size if length is None else int(length)
        nrm = self.nrm[:L]
        acc = self.acc[:L]
        # 边界保护：帧覆盖不到的地方 nrm 会趋近 0，acc/nrm 会炸成几十倍。
        # 这些位置本来就没有可用的重构结果，直接置 0（调用方靠补零把
        # 有效区间推到这里面，见 process 里的 pad）。
        nz = nrm > 1e-6
        if np.any(nz):
            ref = float(np.percentile(nrm[nz], 90))
            bad = nrm < 0.25 * ref
            acc = np.where(bad, 0.0, acc)
            nrm = np.where(bad, 1.0, nrm)
        return acc / np.maximum(nrm, 1e-9)


# ---------------------------------------------------------------- A 步：TSM

def _tsm(x, n, hop, w, ratio, progress=None):
    """相位声码器时间伸缩：时长 × ratio，音高与共振峰都不变。

    合成相位按 Laroche-Dolson 递推：

        标称相位推进   dphi_nom(k) = 2*pi*k*hop/n
        实测相位推进   dphi(k)     = wrap(phi_m(k) - phi_{m-1}(k))
        瞬时频率偏差   dom(k)      = wrap(dphi(k) - dphi_nom(k))
        合成推进       advance(k)  = ratio * (dphi_nom(k) + dom(k))

    注意是 ratio 乘在**整个**推进量上，不是只乘偏差。只乘偏差的话
    等价于把标称频率也一起搬走，那就变成变调而不是变速了。
    """
    nx = x.size
    nb = n // 2 + 1
    k = np.arange(nb, dtype=np.float64)
    dphi_nom = 2.0 * np.pi * k * hop / float(n)

    m = 1 + (nx - n) // hop
    idx = np.arange(n)
    L = int(np.ceil((nx - 1) * ratio)) + n + 4
    ola = _OLA(n, w, L)

    ph_prev = None
    ps_prev = None
    for s in range(0, m, CHUNK_FRAMES):
        e = min(s + CHUNK_FRAMES, m)
        off = hop * np.arange(s, e, dtype=np.int64)
        fr = x[off[:, None] + idx[None, :]] * w[None, :]
        X = np.fft.rfft(fr, n=n, axis=1)
        A = np.abs(X)
        ph = np.angle(X)

        blk = e - s
        dom = np.empty((blk, nb), dtype=np.float64)
        dom[1:] = _wrap_pi(ph[1:] - ph[:-1] - dphi_nom[None, :])
        # 第一帧没有"上一帧"可比：偏差取 0，等价于假设它正落在标称频率上。
        # 跨块时要把上一块最后一帧的相位续上，否则每 2048 帧会有一次
        # 相位跳变，听感是一声轻微的"啪"。
        dom[0] = 0.0 if s == 0 else _wrap_pi(ph[0] - ph_prev - dphi_nom)

        adv = ratio * (dphi_nom[None, :] + dom)
        inc = np.zeros_like(adv)
        inc[1:] = adv[1:]
        base = ph[0][None, :] if s == 0 else ps_prev[None, :]
        ps = base + np.cumsum(inc, axis=0)

        Y = A * np.exp(1j * ps)
        back = np.fft.irfft(Y, n=n, axis=1)
        for i in range(blk):
            ola.add(back[i], ratio * hop * (s + i))

        ph_prev = ph[-1].copy()
        ps_prev = ps[-1].copy()
        if progress:
            progress(0.55 * e / m)

    return ola.result(L)


# ---------------------------------------------------------------- B 步：重采样

_KERN = {}
_KERN_Q = 4096          # 小数相位的量化级数（误差分析见 _kernel_table）


def _kernel_table(s, nt, span, beta, qlev=_KERN_Q):
    """把"窗口 sinc 核"按小数相位预制成一张表。

    重采样的核只跟两样东西有关：小数相位 frac，和抽头偏移 t。
    原先的实现对每个输出位置都要重算一遍 np.sinc 和 np.i0（Kaiser），
    实测 4 分钟立体声要 190 秒 —— i0 是修正贝塞尔函数，非常慢，
    而它在整条信号上算的其实是同一批值。

    这里把 frac 量化成 qlev 档、把 t 的 2*nt 个偏移摊开，一次性
    算出 (qlev, 2*nt) 的核表缓存起来，之后每个输出位置只是查表。

    量化误差：|Δfrac| ≤ 1/(2*qlev) = 1.22e-4 样本。插值误差约等于
    信号斜率 × Δfrac；48 kHz 下 20 kHz 成分的归一化斜率是 2π*(20/48)
    = 2.62，所以最坏情形的插值误差 ≈ 3.2e-4 ≈ -70 dB，低于 Kaiser
    窗本身的 -90 dB 旁瓣，不是瓶颈。
    """
    key = (round(float(s), 9), int(nt), round(float(span), 9),
           round(float(beta), 6), int(qlev))
    tbl = _KERN.get(key)
    if tbl is not None:
        return tbl

    toff = np.arange(-nt + 1, nt + 1, dtype=np.float64)
    q = (np.arange(qlev, dtype=np.float64) + 0.5) / float(qlev)
    d = q[:, None] - toff[None, :]          # (qlev, 2nt)
    tbl = np.ascontiguousarray(np.sinc(d / s) * _kaiser(d / span, beta) / s)

    if len(_KERN) > 8:
        _KERN.clear()
    _KERN[key] = tbl
    return tbl


def _sinc_resample_uniform(src, start, step, m, s, nt, span, beta):
    """等间距位置的快速通路：查核表 + 分块 gather。

    零填充的左右宽度是按实际索引范围算出来的，保证"越界的抽头一定落在
    零区里"，这样 gather 之后不需要再做一次布尔掩膜（掩膜会让临时内存
    翻倍）。
    """
    tbl = _kernel_table(s, nt, span, beta)
    qlev, K = tbl.shape
    toff = np.arange(-nt + 1, nt + 1, dtype=np.int64)

    pos = start + step * np.arange(m, dtype=np.float64)
    i0 = np.floor(pos).astype(np.int64)
    frac = pos - i0
    qi = np.rint(frac * qlev - 0.5).astype(np.int64)
    np.clip(qi, 0, qlev - 1, out=qi)

    n_in = src.size
    bmin = int(i0.min()) if m else 0
    bmax = int(i0.max()) if m else 0
    # 左零区要能吃掉 i0+(-nt+1) 的最小可能值；右零区同理
    pl = max(1, nt - bmin)
    pr = max(1, bmax + nt - (pl + n_in) + 1)
    sp = np.empty(pl + n_in + pr, dtype=np.float64)
    sp[:pl] = 0.0
    sp[pl:pl + n_in] = src
    sp[pl + n_in:] = 0.0
    hi = sp.size - 1

    base = i0 + pl
    out = np.zeros(m, dtype=np.float64)
    chunk = 1 << 17
    buf = np.empty(chunk, dtype=np.int64)
    for b in range(0, m, chunk):
        e = min(m, b + chunk)
        c = e - b
        bb = base[b:e]
        w = tbl[qi[b:e]]                       # (c, K)
        acc = np.zeros(c, dtype=np.float64)
        sl = buf[:c]
        for j in range(K):
            np.add(bb, toff[j], out=sl)
            np.clip(sl, 0, hi, out=sl)
            acc += sp[sl] * w[:, j]
        out[b:e] = acc
    return out


def _sinc_resample_general(src, pos, s, nt, span, beta):
    """非等间距位置的通路（逐个位置现算核）。慢，只在真正需要时用。"""
    n_in = src.size
    i0 = np.floor(pos).astype(np.int64)
    frac = pos - i0
    out = np.zeros(pos.size, dtype=np.float64)
    for t in range(-nt + 1, nt + 1):
        idx = i0 + t
        ok = (idx >= 0) & (idx < n_in)
        if not np.any(ok):
            continue
        d = frac[ok] - t
        h = np.sinc(d / s) * _kaiser(d / span, beta) / s
        out[ok] += src[idx[ok]] * h
    return out


def sinc_resample(src, pos, ratio, taps=RESAMP_TAPS, beta=KAISER_BETA):
    """在任意浮点位置 pos 上对 src 做带限插值。

    ratio > 1 时是在**抽取**（B 步把 A 步拉长的信号压回去），必须先抗混叠：
    重采样核的截止频率要降到 fs/(2*ratio)，否则原本靠近 fs/2 的成分
    会折叠回来变成噪声。做法是把 sinc 核按 ratio 展宽并归一：

        s = max(1, ratio)
        h(u) = (1/s) * sinc(u/s) * kaiser(u / (half*s))

    s = 1 时退化成普通的 sinc(u)*kaiser(u/half)，截止在 fs/2，
    正好是插值（ratio < 1）该有的样子。

    不用线性插值：线性插值的镜像抑制只有 -25 dB 左右，变调后翻上去的
    高次谐波会折叠回来，在齿音和气声上听得很清楚。

    等间距位置会走 _sinc_resample_uniform（查表，快约 20 倍）；
    判据是"实际位置与理想等间距的最大偏差 ≤ 1e-5 样本"，这个量级
    远小于核表本身的量化误差。
    """
    src = np.ascontiguousarray(np.asarray(src, dtype=np.float64))
    pos = np.asarray(pos, dtype=np.float64)
    s = max(1.0, float(ratio))
    span = (taps // 2) * s          # Kaiser 窗的半宽（单位：输入样本）
    nt = int(np.ceil(span))         # 抽头必须覆盖到 ±span

    m = pos.size
    if m == 0:
        return np.zeros(0, dtype=np.float64)
    if m >= 2:
        step = float(pos[1] - pos[0])
        ideal = float(pos[0]) + step * np.arange(m, dtype=np.float64)
        if float(np.max(np.abs(pos - ideal))) <= 1e-5:
            return _sinc_resample_uniform(src, float(pos[0]), step, m,
                                          s, nt, span, beta)
    return _sinc_resample_general(src, pos, s, nt, span, beta)


# ---------------------------------------------------------------- C 步：共振峰校正

def _formant_correct(y, x, sr, n, hop, w, lift, ratio, strength,
                     progress=None):
    """把 y 的频谱包络拉回 x 的频谱包络，逐帧对齐。

        G(m, k) = clip( E_x(m, k) / E_x(m, k / ratio) )

    ★ 分母用的是**原信号包络的频率弯折值**，不是实测的 E_y。

    一开始写的是 G = E_x / E_y（E_y 从变调后的 y 实测），实测下来是错的：
    倒谱包络能把"谐波梳"切掉的前提是谐波间距小于包络分辨率（sr/lift，
    48 kHz 下 375 Hz）。变调之后谐波间距变成 ratio 倍，一旦超过这个
    分辨率，谐波本身就会漏进 E_y，包络上出现假的峰，校正反而把真正的
    内容压掉 —— 单频正弦（440 -> 660）上实测电平被压掉 12 dB。

    而 B 步只是把整条频谱按比例搬移、幅度不变，所以 y 的包络**必然**
    等于 E_x(f/ratio)，可以直接算出来，根本不用测。这样整条链路里只有
    E_x 一个包络需要估计，而 E_x 是在原始基频下测的，谐波间距最小，
    最不容易漏 —— 一举避开了污染问题。

    ratio < 1（降调）时需要 E_x 在 Nyquist 之外的取值，物理上不存在，
    这里按边界值外推（保持端点）。降调时共振峰本来就是往下跑，
    影响比升调小得多。

    ★ 还要在**帧间**做一极点平滑（TAU_ENV = 30 ms）。

    不加这一步是跑不通的：逐帧估计的倒谱包络对"帧起点落在周期的哪个
    相位"很敏感（hop 256 而基频周期常常不是 256 的整数倍），实测
    增益的帧间相对标准差有 22%。增益一帧一抖，等于给信号叠了一个
    帧率（48 kHz / 256 = 187.5 Hz）的调幅，产生一堆边带 ——
    自相关测 F0 直接被骗到 80 Hz（真值 160 Hz 的一半）。
    共振峰是以音节速度（100 ms 量级）变化的，30 ms 的平滑窗口
    对音质毫无影响，却能把增益抖动压到千分之几。

    注意：这里**不改相位**。改相位会破坏 B 步刚建立的瞬时频率关系，
    基频会跑。只乘一个实数增益，相位照搬。
    """
    ny = y.size
    nb = n // 2 + 1
    m = 1 + (ny - n) // hop
    idx = np.arange(n)

    kb = np.arange(nb, dtype=np.float64)
    src = kb / ratio                       # 输出 bin k <- 原包络的 bin k/ratio
    lo = np.clip(np.floor(src).astype(np.int64), 0, nb - 1)
    hi = np.clip(lo + 1, 0, nb - 1)
    wl = (src - lo)[None, :]

    # 一极点 IIR 系数：hop/sr 秒一帧，时间常数 TAU_ENV
    a_env = math.exp(-(float(hop) / float(sr)) / TAU_ENV)
    lg_s = None                            # 跨块延续的平滑状态

    ola = _OLA(n, w, ny)
    for s in range(0, m, CHUNK_ENV):
        e = min(s + CHUNK_ENV, m)
        off = hop * np.arange(s, e, dtype=np.int64)
        sl = off[:, None] + idx[None, :]

        Xa = np.fft.rfft(x[sl] * w[None, :], n=n, axis=1)
        # 截断长度每 CHUNK_ENV 帧重算一次（2.7 秒，跟 F0 的变化速度匹配）
        lf = adaptive_lift(np.log(np.maximum(np.abs(Xa), 1e-10)).mean(0),
                           sr, n, lift)
        ex = cepstral_envelope(np.abs(Xa), lf, n)

        # 帧间平滑（对 log 包络做，等价于对幅度做几何平均）
        lg = np.log(np.maximum(ex, 1e-10))
        lgf = np.empty_like(lg)
        cur = lg[0].copy() if lg_s is None else lg_s
        for i in range(lg.shape[0]):
            cur = a_env * cur + (1.0 - a_env) * lg[i]
            lgf[i] = cur
        lg_s = cur

        ex = np.exp(lgf)
        pred = np.exp((1.0 - wl) * lgf[:, lo] + wl * lgf[:, hi])
        g = np.clip(ex / np.maximum(pred, 1e-10), 1.0 / GMAX, GMAX)
        if strength < 1.0:
            g = 1.0 + strength * (g - 1.0)

        Ya = np.fft.rfft(y[sl] * w[None, :], n=n, axis=1)
        Yb = Ya * g
        back = np.fft.irfft(Yb, n=n, axis=1)
        for i in range(e - s):
            ola.add(back[i], hop * (s + i))
        if progress:
            progress(0.55 + 0.45 * e / m)

    return ola.result(ny)


# ---------------------------------------------------------------- 对外接口

def _stage_count(ratio):
    """把变调比拆成几级做。

    每级不超过 MAX_STAGE_RATIO，级数取够用的最小整数：
        2.00 -> 2 级（1.414 每级）      0.50 -> 2 级（0.707 每级）
        2.52 -> 3 级（1.361 每级）      0.25 -> 3 级（0.630 每级）
    """
    lim = float(MAX_STAGE_RATIO)
    if abs(ratio - 1.0) < 1e-9:
        return 1
    if ratio > 1.0:
        k = int(math.ceil(math.log(ratio) / math.log(lim)))
    else:
        k = int(math.ceil(math.log(ratio) / math.log(1.0 / lim)))
    return max(1, min(8, k))


def process(x, sr, ratio, formant=1.0, n=NFFT, hop=HOP, progress=None):
    """高质量变调。

    x       : 1-D float 数组（单声道）
    sr      : 采样率
    ratio   : 变调比，> 1 升调（2.0 = 升八度，0.5 = 降八度）
    formant : 共振峰保持强度。0 = 不保持（经典花栗鼠），1 = 完全保持
    返回    : 与 x 等长的 float64 数组
    """
    x = np.asarray(x, dtype=np.float64)
    nx = x.size
    if nx == 0:
        return x.copy()
    if abs(ratio - 1.0) < 1e-9:
        return x.copy()

    # ★★ 大变调量必须拆成多级级联，单级不能超过 MAX_STAGE_RATIO。★★
    #
    # 这是实测出来的硬边界，不是保守取值。合成元音（F0 = 120 Hz、
    # 共振峰 700/1200/2500）上扫 (alpha, hop) 网格，量"不在 120 Hz
    # 整数倍上的谱能量占比"：
    #
    #     alpha = 1.25 / 1.335 / 1.50  ->  0.005 / 0.010 / 0.019   干净
    #     alpha = 1.75                 ->  0.058 ~ 0.146           开始崩
    #     alpha = 2.00                 ->  0.34  ~ 0.52            完全坏掉
    #
    # 坏掉的表现是凭空多出 F0/2 的次谐波：alpha = 2 时自相关在
    # 120 Hz 的 lag 上是 0.9985、在 240 Hz 的 lag 上只有 0.319，
    # 也就是"变调根本没生效"。加大窗长（N = 2048 / 4096）和减小
    # 分析跳距（hop = 64 / 128）都救不回来，所以不是重叠数的问题。
    #
    # 原因在相位声码器本身：合成相位每帧推进 alpha*(dphi_nom + dom)，
    # 高频 bin 的 dphi_nom 本来就接近 pi，乘上大 alpha 之后推进量
    # 远超一个周期，重构出来的就不是"同一条正弦被拉长"，而是一片
    # 相位不相干的东西。
    #
    # 级联是标准做法，而且开销可控：alpha = 2 拆成 1.414 × 1.414，
    # 两级都在安全区里。
    k = _stage_count(ratio)
    each = ratio ** (1.0 / k)

    # 单级 ratio 已经 <= MAX_STAGE_RATIO，正常情况下这一条不会触发，
    # 留着是给"调用方直接改常量"的兜底。
    while hop > 32 and each * hop > n / 2.5:
        hop >>= 1

    if nx < n + 2 * hop:
        # 太短，分不出一帧，直通（调用方那里也是直通）
        return x.copy()

    w = hann_periodic(n)

    # 首尾各补 n 个零。
    # 重叠相加在信号两端是"半张窗"：最后一个样本只有最后一帧的一点点
    # 窗权重（Hann 末端 w→0），归一化时 acc/nrm 会炸。补零等于给两端
    # 各多造 n/hop 帧，让有效区间落在完整的 4 帧覆盖里，算完再剪掉。
    # 代价是多算 2n 个样本，换来的是干净的边界。
    pad = n
    xp = np.concatenate([np.zeros(pad, dtype=np.float64), x,
                         np.zeros(pad, dtype=np.float64)])

    # A + B，做 k 级
    y = x
    for si in range(k):
        yp = np.concatenate([np.zeros(pad, dtype=np.float64), y,
                             np.zeros(pad, dtype=np.float64)])
        x_tsm = _tsm(yp, n, hop, w, each, progress)
        pos = (pad + np.arange(y.size, dtype=np.float64)) * each
        y = sinc_resample(x_tsm, pos, each)

    # C：共振峰校正只做一次，而且是相对**原始** x 的包络。
    # 逐级做的话，第二级会把第一级的输出当成"新的原声"，校正目标就错了。
    if formant > 1e-6:
        lift = max(8, int(round(LIFT_MS * 1e-3 * float(sr))))
        yp = np.concatenate([np.zeros(pad, dtype=np.float64), y,
                             np.zeros(pad, dtype=np.float64)])
        y = _formant_correct(yp, xp, sr, n, hop, w, lift, ratio, formant,
                             progress)[pad:pad + nx]
    elif progress:
        progress(1.0)
    return np.asarray(y[:nx], dtype=np.float64)


class HQVoiceShifter(object):
    """按声道独立处理的高质量变调器（与 dsp_core.StereoChain 的用法一致）。

    纯离线：一次喂一整段。实时链路请继续用 dsp_core.PitchShifter。
    """

    def __init__(self, sr=48000):
        self.sr = int(sr)

    def reset(self):
        pass                      # 无状态：每次 process 都是完整的一段

    def process(self, x, ratio, formant=1.0, progress=None):
        a = np.asarray(x, dtype=np.float64)
        if a.ndim == 1:
            return process(a, self.sr, ratio, formant, progress=progress)
        out = np.empty_like(a)
        for c in range(a.shape[1]):
            out[:, c] = process(np.ascontiguousarray(a[:, c]), self.sr,
                                ratio, formant, progress=progress)
        return out


def available():
    """只依赖 numpy，永远可用。留这个接口是为了和 local_ai 的后端风格统一。"""
    return True, ""
