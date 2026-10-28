# -*- coding: utf-8 -*-
"""
vocal_route.py —— 把一路"人声"按男 / 女分到左右两轨

定位
----
只负责分工的后半段：**给定已经提干净的人声**（demucs 的 vocals 轨，
或者纯 DSP 的中置提取结果），把它拆成"男声"和"女声"两条单声道 + 伴奏。

为什么基频用自相关而不是谐波积
------------------------------
在干净人声上实测（`tool/dbg_pitch2.py`）：

    方法        男声独唱中位    女声独唱中位
    自相关        120.7 Hz       234.6 Hz     ← 对
    谐波积(HPS)   314.3 Hz       263.2 Hz     ← 错

原因是真人的谐波能量集中在 300~3000 Hz 的共振峰上，**基波往往很弱**；
谐波积带 1/h 权重，基波一弱就被倍频盖过去。自相关看的是"整段波形的
周期重复性"，不依赖基波强弱，所以在干净人声上稳得多。

两人同时唱怎么办
----------------
先在"低音区"和"高音区"各找一条自相关峰，再用**残差解释力**判断
第二位是不是另一个人（详见 `_pick_two`）。两人谐波位置完全重合时
（例如严格差八度）物理上就分不开，这时退化成按能量比例分配，
不会把整段人声错误地全推到一侧。
"""

import numpy as np

NFFT = 1024
HOP = 256
F0WIN = 2048
OLA = 2.0 / 3.0

#: 平滑 / 决策
HIST = 9                 # 基频中值滤波帧数
HANG = 12                # 丢掉基频后保持判定的帧数（约 70 ms）
PRESENCE = 0.45          # 第二位歌手相对第一位的"新增解释力"门槛
REL_TOL = 0.03           # 两个基频接近整数倍到这个程度就当成同一人
OCT_TOL = 0.06           # 八度折叠容差：两候选成 2:1 到这个程度就当同一个人的两个倍频
OCT_FOLD = True          # 八度折叠总开关（置 False 可退回旧行为做 A/B 对比实测）

BAND = 0.03              # 谐波梳的带宽（相对）
HARM_MAX = 11000.0
HI_HZ = 5000.0           # 以上给齿音留一条宽带通道


def _hann(n):
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / float(n))).astype(np.float64)


_W = _hann(NFFT)
_W2 = _hann(F0WIN)


# ---------------------------------------------------------------- 基频

def _acf_f0(seg, sr, fmin, fmax, whiten_hz=0.0, whiten_cap=8.0):
    """归一化自相关找最低的那个强峰（抗倍频）。

    whiten_hz > 0 时先做**谱白化**再取自相关：把幅度谱除以它自己的滑动
    平均包络（宽度 whiten_hz），凸出谐波峰、压掉宽带伴奏背景。

    ★ 这一条是 2026-10-21 用真实歌曲校准出来的最大杠杆。在混音上直接
      取自相关时，伴奏的宽带能量把周期峰糊平，基频正确率只有 22.6%；
      加 250 Hz 白化后升到 41.8%，最终分离度 SIR 从 +1.49 dB 到 +12.56 dB
      （见 tool/calib_f0.py）。在**干净人声**上白化没有明显收益，所以
      vocal_route 自己的调用保持默认 0，只有 vocal_split 处理混音时才开。
    """
    w = (seg - seg.mean()) * _W2[:len(seg)]
    n = len(w)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    S = np.fft.rfft(w, nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    if whiten_hz > 0.0:
        bw = max(3, int(round(whiten_hz / (sr / float(nfft)))))
        mag = np.abs(S)
        env = np.convolve(mag, np.ones(bw) / bw, mode="same")
        # ★ 用"乘增益"而不是"除包络"：两者在 ACF 上不等价 ——
        #   除包络得到 (|S|/env)²，乘增益得到 (|S|²/env)²，后者对强峰更敏感。
        #   更要紧的是必须给增益**设上限**：窄峰除以 21 bin 的滑动均值会被
        #   放大 10~20 倍，连 Hann 旁瓣也一起放大，在**无噪声的纯音**上会
        #   造出伪周期性 —— 实测 231 Hz 被报成 243.7 Hz（+5.5%），220 Hz
        #   报成 242.3 Hz（+10%）。盖上 4 倍（+12 dB）的顶就正常了。
        g = mag / (env + 1e-9)
        np.clip(g, 0.0, float(whiten_cap), out=g)
        S = S * g
    S[(f < fmin * 0.7) | (f > 3000.0)] = 0.0
    acf = np.fft.irfft(S * np.conj(S), nfft)[:n]
    if acf[0] <= 0:
        return 0.0, 0.0
    acf = acf / acf[0]
    lo = max(2, int(sr / (fmax * 1.2)))
    hi = min(n - 1, int(sr / (fmin * 0.92)))
    if hi <= lo + 2:
        return 0.0, 0.0
    r = acf[lo:hi + 1]
    gmax = float(r.max())
    if gmax <= 0.0:
        return 0.0, 0.0
    # 第一个"够强的局部极大"：宁可报低音，也不要被倍频骗
    d = np.diff(r)
    cand = np.where((d[:-1] > 0) & (d[1:] <= 0))[0] + 1
    if cand.size:
        cand = cand[r[cand] >= 0.85 * gmax]
    idx = int(cand[0]) if cand.size else int(np.argmax(r))
    lag = float(lo + idx)
    if 0 < idx < r.size - 1:
        a, b, c = float(r[idx - 1]), float(r[idx]), float(r[idx + 1])
        den = a - 2 * b + c
        if abs(den) > 1e-12:
            lag += 0.5 * (a - c) / den
    return (float(sr) / lag if lag > 0 else 0.0), float(r[idx])


def _comb(f, f0):
    hm = np.zeros_like(f)
    if f0 <= 0:
        return hm
    h = 1
    while h * f0 < HARM_MAX and h <= 48:
        fc = float(h * f0)
        bw = max(35.0, fc * BAND)
        np.maximum(hm, np.clip(2.0 - np.abs(f - fc) / bw, 0.0, 1.0), out=hm)
        h += 1
    return hm


def _peak_mag(mag, f, sr, nfft=NFFT):
    """取频率 f 附近的谱峰幅度（rfft bin 邻域取最大）。"""
    if f <= 0 or mag.size == 0:
        return 0.0
    i = int(round(f * nfft / float(sr)))
    if i >= mag.size:
        return 0.0
    return float(mag[max(0, i - 1):min(mag.size, i + 2)].max())


def resolve_octave(fm, ff, mag, sr, nfft=NFFT):
    """两个基频候选成 2:1 时，判定是"一个人的两个倍频"还是"真有两个人"。

    返回 (fm, ff)，被否掉的那个置 0。

    为什么不能简单"取显著度高的"：自相关对 **f0/2** 和 **2×f0** 都会出峰，
    两个方向的错误同时存在——
        独唱女声 231 Hz  → 男声区报 115.8 Hz（半频错误）
        独唱男声 110 Hz  → 女声区报 220 Hz（倍频错误）
    只看显著度必错一半（实测 bench_split 上男声轨 SI-SDR 掉到 -11.6 dB）。

    改用**低候选的基频位置本身有没有谱峰**这个物理判据，但只在能确定
    低候选是假的时候才动手：

        低候选处**没有**谱峰  -> 它是 f0/2 的半频假峰，必须丢掉，取高的
            （独唱女声 232 Hz 被男声区报成 116 Hz 就是这种）
        低候选处**有**谱峰    -> 可能是真的八度齐唱（男 118 + 女 232），
            也可能只是男声的二次谐波被当成第二个人。这两种从"有没有能量"
            上分不出来，而**折叠的代价不对等**：把齐唱折叠掉会整段丢一个
            人（实测女声轨 SI-SDR 掉 2.4 dB），把谐波当人只是漏一点能量。
            所以这种情况保持原样，交给后面的 both 判定去处理。
    """
    if fm <= 0 or ff <= 0 or not OCT_FOLD:
        return fm, ff
    r = ff / fm
    if not (abs(r - 2.0) / 2.0 < OCT_TOL
            or abs(1.0 / r - 2.0) / 2.0 < OCT_TOL):
        return fm, ff
    fm_lt_ff = fm < ff
    lo, hi = (fm, ff) if fm_lt_ff else (ff, fm)
    if _peak_mag(mag, lo, sr, nfft) >= 0.5 * _peak_mag(mag, hi, sr, nfft):
        return fm, ff                      # 低候选处确有能量：不动，保持原样
    return (0.0, hi) if fm_lt_ff else (hi, 0.0)


def _median(vals):
    vals = [x for x in vals if x > 0]
    return float(np.median(vals)) if len(vals) >= 3 else 0.0


# ---------------------------------------------------------------- 主函数

def route_vocal(v, sr, split=165.0, depth=1.0, f0_min=70.0, f0_max=500.0,
                progress=None, stats=None):
    """一路人声 -> (男声轨, 女声轨)，都是单声道、等长。

    split  男 / 女音域分界（Hz）。两个搜索区间**故意重叠**，
           免得唱到边界的歌手整个人消失。
    depth  0..1，从对侧扣除多少（1 = 分得最开）。
    """
    v = np.asarray(v, dtype=np.float64)
    n = v.size
    male = np.zeros(n)
    female = np.zeros(n)
    if n < NFFT * 2:
        return male.astype(np.float32), female.astype(np.float32)

    f = np.fft.rfftfreq(NFFT, 1.0 / sr)
    # 两个搜索区间只留很窄的重叠：重叠太多的话，男声也会落进"女声区"，
    # 两个候选指向同一个峰，决策就全靠运气了。
    lo_m, hi_m = f0_min, split * 1.06
    lo_f, hi_f = split * 0.94, f0_max
    hi_shelf = np.clip((f - HI_HZ) / 3000.0, 0.0, 1.0) * 0.6

    nfr = max(0, (n - NFFT) // HOP + 1)
    hist = []
    n_male = n_female = n_both = n_none = 0

    for k in range(nfr):
        s = k * HOP
        seg = v[s:s + NFFT]
        S = np.fft.rfft(seg * _W)
        mag = np.abs(S)

        # 基频分析窗：2048 点，尾端对准本帧末尾
        e2 = s + HOP
        st2 = e2 - F0WIN
        if st2 >= 0:
            fw = v[st2:e2]
            fm_raw, sm = _acf_f0(fw, sr, lo_m, hi_m)
            ff_raw, sf = _acf_f0(fw, sr, lo_f, hi_f)
        else:
            fm_raw = ff_raw = 0.0
            sm = sf = 0.0
        hist.append((fm_raw, ff_raw))
        if len(hist) > HIST:
            hist.pop(0)
        fm = _median([x[0] for x in hist])
        ff = _median([x[1] for x in hist])

        # ---- 八度折叠：两候选成 2:1 时按"低候选处有没有谱峰"二选一 ----
        # （规则与实现都在 resolve_octave 里，实时路径共用同一份）
        fm, ff = resolve_octave(fm, ff, mag, sr)

        # 第二位到底是不是"另一个人"：看它在第一位解释不了的残差里
        # 还能捞到多少能量
        Hm = _comb(f, fm) if fm > 0 else None
        Hf = _comb(f, ff) if ff > 0 else None
        both = False
        if Hm is not None and Hf is not None:
            Em = float((Hm * mag).sum())
            Ef = float((Hf * mag).sum())
            r_m = mag * (1.0 - np.minimum(1.0, Hm))
            r_f = mag * (1.0 - np.minimum(1.0, Hf))
            Ef_new = float((Hf * r_m).sum())
            Em_new = float((Hm * r_f).sum())
            # 谐波位置几乎完全重合（严格八度）时谁也解释不了新东西 ——
            # 这时按"两个人都在唱"处理，能量对半分，好过整段推错边。
            ratio = max(fm, ff) / max(min(fm, ff), 1e-9)
            nr = round(ratio)
            octave_locked = (nr >= 2 and abs(ratio - nr) / nr < REL_TOL)
            both = (Ef_new > PRESENCE * Em and Em_new > PRESENCE * Ef) or octave_locked

        if fm > 0 and ff > 0 and both:
            tot = Hm + Hf
            sh = sm / (sm + sf + 1e-9)
            mm = np.where(tot > 1e-9, Hm / (tot + 1e-9), 0.0)
            mf = np.where(tot > 1e-9, Hf / (tot + 1e-9), 0.0)
            mm = mm * (1.0 - hi_shelf) + hi_shelf * sh
            mf = mf * (1.0 - hi_shelf) + hi_shelf * (1.0 - sh)
            n_both += 1
        elif fm > 0 and (ff <= 0 or sm >= sf):
            mm = Hm * (1.0 - hi_shelf) + hi_shelf
            mf = np.zeros_like(mm)
            n_male += 1
        elif ff > 0:
            mf = Hf * (1.0 - hi_shelf) + hi_shelf
            mm = np.zeros_like(mf)
            n_female += 1
        else:
            mm = mf = np.zeros_like(f)
            n_none += 1

        if depth < 1.0:
            mm = mm * depth
            mf = mf * depth

        male[s:s + NFFT] += np.fft.irfft(mm * S, NFFT) * _W * OLA
        female[s:s + NFFT] += np.fft.irfft(mf * S, NFFT) * _W * OLA
        if progress is not None and (k & 255) == 0:
            progress(k, nfr)

    if stats is not None:
        tot = max(1, n_male + n_female + n_both + n_none)
        stats.update(male_pct=100.0 * n_male / tot, female_pct=100.0 * n_female / tot,
                     both_pct=100.0 * n_both / tot, none_pct=100.0 * n_none / tot)
    return male.astype(np.float32), female.astype(np.float32)
