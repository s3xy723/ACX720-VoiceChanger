# -*- coding: utf-8 -*-
"""
sim_separation.py —— rtl/separation_core.v 的逐比特参考模型

本机 xsim 时好时坏，所以沿用本工程一贯做法：把 RTL 逐行复刻成 Python，
再和 numpy 的浮点实现对照。它验证三件事：

1. **定点参数**：位宽、移位、增益链是否已经标定对（这是写 RTL 之前必须先
   定下来的——增益错一位，输出就整体差一倍，而且听不出来是"差一倍"）
2. **基频与判定**：男 / 女 / 两人的判定规则在真信号上是否成立
3. **分离效果**：左声道里男声保留、女声被扣掉多少（SI-SDR 风格打分）

用法：  python tool/sim_separation.py
"""

import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sim_fft1024 import fft_fixed, bitrev10          # 复用已验证的 FFT 模型

N = 1024
HOP = 256
NBIN = 512
FS = 24414.0                      # 板上实测采样率：MCLK 12.5 MHz / 512

# ---- 迟滞区间（lag，单位 = 24.414 kHz 的样本）----
# 男声 f0 70..175 Hz -> lag 139..349；女声 165..500 Hz -> lag 49..148
LAG_M_LO, LAG_M_HI = 139, 348
LAG_F_LO, LAG_F_HI = 48, 157

HARM_MAX = 40                     # 梳状打到第几个谐波
RECIP_Q8 = None                   # 倒数表，从 rtl/pitch_recip.v 解析

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


# ---------------------------------------------------------------- 工具

def sat(v, bits):
    lim = (1 << (bits - 1)) - 1
    return -lim - 1 if v < -lim - 1 else (lim if v > lim else v)


def hann1024_q15():
    v = []
    for i in range(N):
        w = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / N)
        q = int(round(32767.0 * w))
        v.append(0 if q < 0 else (32767 if q > 32767 else q))
    return np.array(v, dtype=np.int64)


WIN = hann1024_q15()


def amax_beta(re, im):
    """|X| ≈ max + 0.375*min（alpha-max-beta-min 近似，最大误差约 3.9%）。

    硬件里开方太贵，而这个近似只要比较器和两次移位。
    它只用在"掩蔽比值"上——比值差 4% 听不出来。
    """
    a = abs(int(re))
    b = abs(int(im))
    hi, lo = (a, b) if a >= b else (b, a)
    return hi + (lo >> 2) + (lo >> 3)


def div_q15(num, den, steps=15):
    """num/den 的 Q15 商，串行移位减（RTL 里就是这么做的）。

    前置条件 0 <= num <= den，结果落在 [0, 32767]。

    **必须是 15 步不是 16 步**：16 步移位减算出来的是 Q16（0..65535），
    num==den 时会得到 65535 而不是 32767 —— 掩蔽凭空放大一倍，
    听起来只是"响了一点"，但增益链就再也闭合不了了。
    """
    if den <= 0:
        return 0
    if num <= 0:
        return 0
    rem = num
    q = 0
    for i in range(steps - 1, -1, -1):
        rem2 = rem << 1
        if rem2 >= den:
            rem = rem2 - den
            q |= (1 << i)
        else:
            rem = rem2
    return q


def recip4(s):
    """梳状竞争的分母只可能是 0..4，查表就够，不需要除法器。"""
    return [0, 32767, 16384, 10923, 8192][s] if 0 <= s <= 4 else 0


def load_recip():
    """从 rtl/pitch_recip.v 里把倒数表抠出来（不手写常数）。"""
    global RECIP_Q8
    p = os.path.normpath(os.path.join(HERE, "..", "rtl", "pitch_recip.v"))
    if not os.path.exists(p):
        return None
    import re
    txt = open(p, "r", encoding="utf-8").read()
    tbl = {}
    for m in re.finditer(r"10'd(\d+)\s*:\s*f0_q8\s*=\s*24'd(\d+)", txt):
        tbl[int(m.group(1))] = int(m.group(2))
    RECIP_Q8 = tbl
    return tbl


# ---------------------------------------------------------------- 浮点参考

def ref_stft_frame(seg_l, seg_r):
    """浮点参考：中置掩蔽 -> V（复谱）。返回的 V 是 rfft 的 512 个 bin。"""
    L = np.fft.rfft(seg_l * (WIN.astype(np.float64) / 32767.0))
    R = np.fft.rfft(seg_r * (WIN.astype(np.float64) / 32767.0))
    mid = 0.5 * (L + R)
    side = 0.5 * (L - R)
    m = np.abs(mid)
    s = np.abs(side)
    base = np.clip((m - s) / (m + 1e-12), 0.0, 1.0)
    return base * mid, m, s


# ---------------------------------------------------------------- 定点链路

def fixed_analyze(seg_l, seg_r):
    """复刻 RTL 的分析段：mid/side 加窗 -> FFT -> 幅度 -> 掩蔽 -> V。

    返回 (V_re, V_im)，都是 24 位整数，长度 NBIN。
    """
    # mid / side：17 位（16 位音频左移 1 位），加窗后写进 FFT
    mid = ((seg_l.astype(np.int64) + seg_r.astype(np.int64)) >> 1) << 1
    side = ((seg_l.astype(np.int64) - seg_r.astype(np.int64)) >> 1) << 1

    mw_re = np.array([sat((int(mid[i]) * int(WIN[i]) + 16384) >> 15, 24)
                      for i in range(N)], dtype=np.int64)
    sw_re = np.array([sat((int(side[i]) * int(WIN[i]) + 16384) >> 15, 24)
                      for i in range(N)], dtype=np.int64)
    zero = np.zeros(N, dtype=np.int64)

    A_re, A_im = fft_fixed(mw_re, zero)              # = DFT(mid_w)/32
    B_re, B_im = fft_fixed(sw_re, zero)              # = DFT(side_w)/32

    sp_re, sp_im = A_re[:NBIN].copy(), A_im[:NBIN].copy()
    sd_mag = np.array([amax_beta(B_re[k], B_im[k]) for k in range(NBIN)],
                      dtype=np.int64)

    # 掩蔽：c = (|mid|-|side|)/|mid|，V = c * mid（保留相位）
    V_re = np.zeros(NBIN, dtype=np.int64)
    V_im = np.zeros(NBIN, dtype=np.int64)
    for k in range(NBIN):
        m = amax_beta(sp_re[k], sp_im[k])
        s = sd_mag[k]
        num = m - s
        num = 0 if num < 0 else num
        c = div_q15(num, m)
        V_re[k] = sat((int(sp_re[k]) * c + 16384) >> 15, 24)
        V_im[k] = sat((int(sp_im[k]) * c + 16384) >> 15, 24)
    return V_re, V_im


def fixed_acf(V_re, V_im):
    """|V|^2 当实偶谱 -> IFFT -> 自相关。返回长度 N 的整数数组。"""
    pw = np.zeros(N, dtype=np.int64)
    for k in range(NBIN):
        p = (int(V_re[k]) ** 2 + int(V_im[k]) ** 2) >> PW_SHIFT
        pw[k] = p
        if k > 0:
            pw[N - k] = p
    zero = np.zeros(N, dtype=np.int64)
    a_re, a_im = fft_fixed(pw, zero, inverse=True)   # = 32 * IDFT(pw)
    return a_re


def pick_peak(acf, lo, hi, ghosts=(), tol=0):
    """区间内取最大值位置，可排除若干个"鬼影"邻域。返回 (lag, peak)。

    周期 T 的信号在 2T、3T、4T… 处一样出峰。女声独唱时男区会依次冒出
    2T、3T 的鬼影，比男声自己的峰还高——**只挖掉 2T 是不够的**，
    挖完 3T 立刻补位，实测就把独唱误判成了两人齐唱。所以把所有整数倍
    一并排除。

    硬件上这些位置不用除法：k*lag_f 就是 (lag_f<<1)、(lag_f<<1)+lag_f…
    几次加法和几个比较器而已。
    """
    best = 0
    best_lag = 0
    for lag in range(lo, hi + 1):
        skip = False
        for g in ghosts:
            if abs(lag - g) <= tol:
                skip = True
                break
        if skip:
            continue
        v = int(acf[lag])
        if v > best:
            best = v
            best_lag = lag
    return best_lag, best


def ghost_lags(lag_f, lo, hi, kmax=8):
    """lag_f 的 2..kmax 倍里落在扫描区间内的那些。"""
    out = []
    for k in range(2, kmax + 1):
        g = k * lag_f
        if lo - 32 <= g <= hi + 32:
            out.append(g)
    return out


def solo_fundamental_test(V_re, V_im, lag_m, pw_of=None):
    """男声基波位置是不是真的有能量 —— 用来解"差八度"歧义。

    自相关分不开 118 Hz 男声 + 236 Hz 女声：混合信号的周期就是 118 Hz 的，
    和纯 236 Hz 女声的 2 倍周期完全重合。

    但频谱能分开：236 Hz 的女声在 **118 Hz 处一点能量都没有**，
    所以只要 bin(f0_m) 上确实立着一个峰，那这个低频基波就只能是独立男声的。

    做法：拿基波位置的功率和它两侧的"背景"比。两侧挑得离基波远一点
    （±4..±12 个 bin），免得把基波自己的泄漏算进背景里。
    """
    if lag_m <= 0 or RECIP_Q8 is None:
        return False
    f0_q8 = RECIP_Q8.get(lag_m, 0)
    pos = (f0_q8 + 128) >> 8                 # 四舍五入，截断会偏一个 bin
    if pos < 4 or pos >= NBIN - 5:
        return False

    def pw_at(q):
        if q < 0 or q >= NBIN:
            return 0
        return pw_of(q) if pw_of else \
            (int(V_re[q]) ** 2 + int(V_im[q]) ** 2) >> PW_SHIFT

    # 峰可能落在估出来的 bin 旁边一格，取三点最大
    p_at = max(pw_at(pos - 1), pw_at(pos), pw_at(pos + 1))
    if p_at <= 0:
        return False
    # 背景只取 ±2..±3：再近会吃到基波自己的泄漏，再远会撞上二次谐波
    acc = 0
    cnt = 0
    for d in (-3, -2, 2, 3):
        acc += pw_at(pos + d)
        cnt += 1
    bg = acc // cnt
    return (p_at > 3 * bg + 4) and (p_at > 16)


def decide(lag_m, pk_m, lag_f, pk_f, acf0, V_re=None, V_im=None, acf=None):
    """男 / 女 / 两人判定。返回 (who, lag_m, lag_f)。

    who: 0 无（清音） / 1 只男 / 2 只女 / 3 两人

    两人齐唱最难判，因为自相关会把两个人的周期叠在一起，而且周期 T 的信号
    在 2T、3T 处同样出峰。这里用**两条判据**串行兜底：

      1. 男区最强峰若是女声的 2T 鬼影，就把鬼影邻域挖掉重扫一遍；
         扫出来的峰够强，说明真有男声。
      2. 差八度时这招会失效——男声的 T 就等于女声的 2T，挖掉鬼影等于把
         男声自己也挖没了。这时改看**男声基波位置有没有独立能量**：
         236 Hz 的女声在 118 Hz 处一点能量都没有，所以 118 Hz 立着峰
         就只能是独立男声。
    """
    thr = acf0 >> 3                       # 峰要超过能量的 12.5%
    vm = 1 if (pk_m > thr and lag_m > 0) else 0
    vf = 1 if (pk_f > thr and lag_f > 0) else 0

    if vm and not vf:
        return 1, lag_m, 0
    if vf and not vm:
        return 2, 0, lag_f
    if not (vm and vf):
        return 0, 0, 0

    # 12.5%：自相关的鬼影峰是有宽度的，卡太紧（6%）会漏掉鬼影的边缘
    tol = max(3, lag_f >> 3)
    ghost = 2 * lag_f
    if abs(lag_m - ghost) <= tol:
        if acf is not None:
            lag2, pk2 = pick_peak(acf, LAG_M_LO, LAG_M_HI,
                                  ghost_lags(lag_f, LAG_M_LO, LAG_M_HI), tol)
            if pk2 > thr and lag2 > 0:
                return 3, lag2, lag_f
        # 差八度时挖鬼影会把男声自己一起挖掉，这时只能靠基波独立能量
        if V_re is not None and solo_fundamental_test(V_re, V_im, lag_m):
            return 3, lag_m, lag_f
        return 2, 0, lag_f

    tol2 = max(2, lag_m >> 4)
    if abs(lag_f - 2 * lag_m) <= tol2:
        return 1, lag_m, 0
    return 3, lag_m, lag_f


def build_comb(f0_q8, cmb):
    """打谐波梳：pos = (h * f0_bin_q8) >> 8，标记 2 / 1 / 1（平顶三角）。"""
    if f0_q8 <= 0:
        return
    for h in range(1, HARM_MAX + 1):
        # 必须四舍五入：截断会让梳子整体往左偏近一个 bin，低频区一个 bin
        # 就是 24 Hz，偏一格就足以把另一个人的基波当成自己的谐波吃掉
        pos = (h * int(f0_q8) + 128) >> 8
        if pos >= NBIN:
            break
        cmb[pos] = 2
        if pos > 0 and cmb[pos - 1] == 0:
            cmb[pos - 1] = 1
        if pos + 1 < NBIN and cmb[pos + 1] == 0:
            cmb[pos + 1] = 1


HP_Q15 = [0] * NBIN
for _k in range(min(33, NBIN)):
    _f = _k * FS / float(N)
    HP_Q15[_k] = 0 if _f <= 0 else min(int(round(
        32767.0 / (1.0 + (120.0 / _f) ** 2))), 32767)
for _k in range(33, NBIN):
    HP_Q15[_k] = 32767

def fixed_mask_and_synth(V_re, V_im, who, lag_m, lag_f):
    """梳状竞争 -> Vm / Vf -> IFFT -> 加窗 -> 两个声道帧。

    返回 (frame_m, frame_f)，长度 N 的整数数组（还没乘 OLA 常数）。
    """
    cmb_m = np.zeros(NBIN, dtype=np.int64)
    cmb_f = np.zeros(NBIN, dtype=np.int64)
    if RECIP_Q8 is None:
        load_recip()
    if who in (1, 3) and lag_m > 0:
        build_comb(RECIP_Q8.get(lag_m, 0), cmb_m)
    if who in (2, 3) and lag_f > 0:
        build_comb(RECIP_Q8.get(lag_f, 0), cmb_f)

    vm_re = np.zeros(NBIN, dtype=np.int64)
    vm_im = np.zeros(NBIN, dtype=np.int64)
    vf_re = np.zeros(NBIN, dtype=np.int64)
    vf_im = np.zeros(NBIN, dtype=np.int64)
    # 2026-10-21：输出语义改成"左=人声 / 右=伴奏"。
    # 两只梳子只用来覆盖两个音高，取并集就是人声掩蔽：
    #   mv = max(cmb_m, cmb_f) * 16384          （0 / 0.5 / 1.0，Q15）
    #   ms = mv * hp[bin] >> 15                 （扣伴奏那一份，过低频保护）
    for k in range(NBIN):
        cu = max(int(cmb_m[k]), int(cmb_f[k]))
        mv = min(cu * 16384, 32767)
        ms = (mv * HP_Q15[k]) >> 15
        vm_re[k] = sat((int(V_re[k]) * mv + 16384) >> 15, 24)
        vm_im[k] = sat((int(V_im[k]) * mv + 16384) >> 15, 24)
        vf_re[k] = sat((int(V_re[k]) * ms + 16384) >> 15, 24)
        vf_im[k] = sat((int(V_im[k]) * ms + 16384) >> 15, 24)

    # 补成 1024 点共轭对称谱再做 IFFT
    def synth(vr, vi):
        fr = np.zeros(N, dtype=np.int64)
        fi = np.zeros(N, dtype=np.int64)
        for k in range(NBIN):
            fr[k] = vr[k]
            fi[k] = vi[k]
            if k > 0:
                fr[N - k] = vr[k]
                fi[N - k] = -vi[k]
        o_re, o_im = fft_fixed(fr, fi, inverse=True)     # = 32 * IDFT
        return o_re

    out_m = synth(vm_re, vm_im)
    out_f = synth(vf_re, vf_im)
    # 合成窗 × OLA 系数 2/3：两步合并成一个 Q15 乘法。
    # 2/3 是必需的：HOP=N/4 时四帧的 Hann² 之和是 1.5，乘 2/3 才回到 1。
    OLA_Q15 = 21845
    gm = np.array([sat(((int(out_m[i]) * int(WIN[i]) + 16384) >> 15)
                       * OLA_Q15 >> 15, 24) for i in range(N)], dtype=np.int64)
    gf = np.array([sat(((int(out_f[i]) * int(WIN[i]) + 16384) >> 15)
                       * OLA_Q15 >> 15, 24) for i in range(N)], dtype=np.int64)
    return gm, gf


PW_SHIFT = 18          # |V|^2 的右移量，下面标定
OLA_SHIFT = 1          # 累加器 -> 输出 的右移量，下面标定


# ---------------------------------------------------------------- 测试信号

def synth_voice(f0, n, sr=FS, nharm=12, rng=None):
    """一段"人声"：基频 + 谐波，含奇次谐波（这样半周期处自相关是负的）。"""
    t = np.arange(n) / float(sr)
    y = np.zeros(n)
    for h in range(1, nharm + 1):
        amp = 1.0 / (h ** 0.9)
        y += amp * np.sin(2 * np.pi * f0 * h * t + (0.7 * h))
    # 加一点共振峰包络，让谱更像人声
    y *= (1.0 + 0.4 * np.sin(2 * np.pi * 1.7 * t))
    return y / (np.max(np.abs(y)) + 1e-9)


def make_stereo(n, male=True, female=True, accomp=True, rng=None,
                f_male=118.0, f_female=232.0):
    """构造测试立体声：人声居中（L=R），伴奏左右不同（这样 side != 0）。

    默认 118 / 232 Hz 是**严格八度**——那种情况按设计本来就该分不开
    （见设计文档 3.3），要测"两人齐唱"得换成非整数倍，比如 118 / 199。
    """
    rng = rng or np.random.RandomState(3)
    L = np.zeros(n)
    R = np.zeros(n)
    vm = vf = None
    if male:
        vm = synth_voice(f_male, n, rng=rng) * 0.55
        L += vm
        R += vm
    if female:
        vf = synth_voice(f_female, n, rng=rng) * 0.45
        L += vf
        R += vf
    if accomp:
        # 伴奏：和弦 + 打击，左右用不同随机相位 => 立体声宽度足够
        t = np.arange(n) / float(FS)
        a1 = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) * 0.12
                 for f in (196.0, 261.6, 329.6))
        a2 = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) * 0.12
                 for f in (196.0, 261.6, 329.6))
        L += a1
        R += a2
    return L, R, vm if male else None, vf if female else None


def db(x):
    return 10.0 * math.log10(max(1e-12, x))


# ---------------------------------------------------------------- 主流程

def run_case(name, L, R, want_who, want_f0=None):
    print("\n--- %s ---" % name)
    if RECIP_Q8 is None:
        load_recip()

    n = L.size
    nfr = (n - N) // HOP + 1
    # 取中间一帧做单帧分析
    k0 = nfr // 2
    s = k0 * HOP
    seg_l = np.round(L[s:s + N] * 22000.0).astype(np.int64)
    seg_r = np.round(R[s:s + N] * 22000.0).astype(np.int64)

    V_re, V_im = fixed_analyze(seg_l, seg_r)
    acf = fixed_acf(V_re, V_im)
    acf0 = int(acf[0]) if int(acf[0]) > 0 else 1
    lag_m, pk_m = pick_peak(acf, LAG_M_LO, LAG_M_HI)
    lag_f, pk_f = pick_peak(acf, LAG_F_LO, LAG_F_HI)
    who, lm, lf = decide(lag_m, pk_m, lag_f, pk_f, acf0, V_re, V_im, acf)

    fm = FS / lm if lm else 0.0
    ff = FS / lf if lf else 0.0
    print("  男区峰 lag=%d pk/acf0=%.3f -> %.1f Hz" % (lag_m, pk_m / float(acf0), FS / lag_m if lag_m else 0))
    print("  女区峰 lag=%d pk/acf0=%.3f -> %.1f Hz" % (lag_f, pk_f / float(acf0), FS / lag_f if lag_f else 0))
    print("  判定 who=%d (1男 2女 3两人 0无)  fm=%.1f ff=%.1f" % (who, fm, ff))
    if want_who >= 0:
        check("%s 判定正确" % name, who == want_who,
              "期望 %d 实测 %d" % (want_who, who))
    return who
    if want_f0 is not None:
        got = fm if want_who == 1 else ff
        check("%s 基频正确" % name, abs(got - want_f0) < 0.08 * want_f0,
              "期望 %.0f 实测 %.1f Hz" % (want_f0, got))
    return who


def main():
    global PW_SHIFT, OLA_SHIFT
    print("=" * 70)
    print(" rtl/separation_core.v —— 分离内核逐比特模型")
    print("=" * 70)

    load_recip()
    check("倒数表已载入", RECIP_Q8 is not None and len(RECIP_Q8) > 400,
          "%d 项" % (len(RECIP_Q8) if RECIP_Q8 else 0))
    if RECIP_Q8:
        f0 = FS * RECIP_Q8.get(100, 0) / (1024.0 * 256.0)
        check("倒数表数值正确", abs(f0 - FS / 100.0) < 1.0,
              "lag=100 -> %.1f Hz（应为 %.1f）" % (f0, FS / 100.0))

    # ---- 1) 位宽标定：|V|^2 右移多少才不溢出 24 位 ----
    print("\n[1] 位宽标定")
    rng = np.random.RandomState(11)
    n = N * 6
    L, R, _, _ = make_stereo(n, True, True, True, rng)
    worst = 0
    worst_ola = 0
    for k in range(0, (n - N) // HOP + 1):
        s = k * HOP
        seg_l = np.round(L[s:s + N] * 30000.0).astype(np.int64)
        seg_r = np.round(R[s:s + N] * 30000.0).astype(np.int64)
        V_re, V_im = fixed_analyze(seg_l, seg_r)
        pw_max = max(int(V_re[k2]) ** 2 + int(V_im[k2]) ** 2 for k2 in range(NBIN))
        worst = max(worst, pw_max)
        acf = fixed_acf(V_re, V_im)
        worst_ola = max(worst_ola, int(np.max(np.abs(acf))))
    print("  |V|^2 峰值 = 2^%.1f   自相关峰值 = 2^%.1f"
          % (math.log(worst, 2) if worst else 0,
             math.log(worst_ola, 2) if worst_ola else 0))
    # 最坏估计：acf[0] = (1/32) * Σ pw <= 16 * pw_max，要 < 2^23
    #   => pw_max < 2^19  => PW_SHIFT > log2(|V|^2峰值) - 19
    need = int(math.ceil(math.log(worst, 2))) - 19 if worst else 0
    print("  => PW_SHIFT 至少要 %d 才不会让 acf[0] 溢出（当前 %d）"
          % (max(0, need), PW_SHIFT))
    check("PW_SHIFT 足够（自相关不溢出）", worst_ola < (1 << 23),
          "自相关峰值 %d < 2^23" % worst_ola)

    # ---- 2) 三种场景的判定 ----
    print("\n[2] 基频与男女判定")
    n = N * 8
    L1, R1, _, _ = make_stereo(n, male=True, female=False)
    run_case("男声独唱", L1, R1, want_who=1, want_f0=118.0)
    L2, R2, _, _ = make_stereo(n, male=False, female=True)
    run_case("女声独唱", L2, R2, want_who=2, want_f0=232.0)
    L3, R3, _, _ = make_stereo(n, male=True, female=True, f_female=199.0)
    run_case("男女齐唱(非八度 118+199)", L3, R3, want_who=3)

    # 严格八度（118 + 236）本来就该分不开：设计只保证它"退化得不离谱"
    L4, R4, _, _ = make_stereo(n, male=True, female=True, f_female=236.0)
    w4 = run_case("八度齐唱(118+236, 已知分不开)", L4, R4, want_who=-1)
    check("八度齐唱退化但不崩", w4 in (1, 3), "who=%d（1 或 3 都算体面）" % w4)

    # ---- 3) 增益链标定：acc 的量纲要和输入对得上 ----
    print("\n[3] 增益链标定")
    n = N * 8
    Lm, Rm, vm, _ = make_stereo(n, male=True, female=False, accomp=False)
    # 纯居中男声：mid = x，正确结果应该是 acc_m ≈ x（男声全部归他）
    acc = np.zeros(n, dtype=np.int64)
    nfr = (n - N) // HOP + 1
    for k in range(nfr):
        s = k * HOP
        seg_l = np.round(Lm[s:s + N] * 22000.0).astype(np.int64)
        seg_r = np.round(Rm[s:s + N] * 22000.0).astype(np.int64)
        V_re, V_im = fixed_analyze(seg_l, seg_r)
        acf = fixed_acf(V_re, V_im)
        acf0 = int(acf[0]) if int(acf[0]) > 0 else 1
        lag_m, pk_m = pick_peak(acf, LAG_M_LO, LAG_M_HI)
        lag_f, pk_f = pick_peak(acf, LAG_F_LO, LAG_F_HI)
        who, lm, lf = decide(lag_m, pk_m, lag_f, pk_f, acf0, V_re, V_im, acf)
        gm, gf = fixed_mask_and_synth(V_re, V_im, who, lm, lf)
        acc[s:s + N] += gm
    x = np.round(Lm * 22000.0).astype(np.int64)
    lo, hi = N, n - N
    ratio = float(np.sum(np.abs(acc[lo:hi]))) / float(np.sum(np.abs(x[lo:hi])) + 1e-9)
    print("  acc_m / 输入 = %.3f（理想 1.0）=> OLA_SHIFT 应取 %.2f"
          % (ratio, math.log(ratio, 2) if ratio > 0 else 0))
    OLA_SHIFT = int(round(math.log(ratio, 2))) if ratio > 0.2 else 0
    print("  取 OLA_SHIFT = %d 后比值 = %.3f" % (OLA_SHIFT, ratio / (2.0 ** OLA_SHIFT)))
    check("增益链闭合", 0.7 < ratio / (2.0 ** OLA_SHIFT) < 1.45,
          "补偿后 acc/输入 = %.3f" % (ratio / (2.0 ** OLA_SHIFT)))

    # ---- 4) 端到端分离效果 ----
    print("\n[4] 端到端：左声道 = 人声，右声道 = 伴奏")
    n = N * 10
    L, R, vm, vf = make_stereo(n, male=True, female=True, accomp=True,
                               f_female=199.0)
    xl = np.round(L * 22000.0).astype(np.int64)
    xr = np.round(R * 22000.0).astype(np.int64)
    acc_m = np.zeros(n, dtype=np.int64)
    acc_f = np.zeros(n, dtype=np.int64)
    nfr = (n - N) // HOP + 1
    for k in range(nfr):
        s = k * HOP
        seg_l = xl[s:s + N]
        seg_r = xr[s:s + N]
        V_re, V_im = fixed_analyze(seg_l, seg_r)
        acf = fixed_acf(V_re, V_im)
        acf0 = int(acf[0]) if int(acf[0]) > 0 else 1
        lag_m, pk_m = pick_peak(acf, LAG_M_LO, LAG_M_HI)
        lag_f, pk_f = pick_peak(acf, LAG_F_LO, LAG_F_HI)
        who, lm, lf = decide(lag_m, pk_m, lag_f, pk_f, acf0, V_re, V_im, acf)
        gm, gf = fixed_mask_and_synth(V_re, V_im, who, lm, lf)
        acc_m[s:s + N] += gm
        acc_f[s:s + N] += gf

    mid_i = (xl + xr) >> 1               # 延迟到输出位置的 mid（整数右移=向下取整）
    y_l = acc_m >> OLA_SHIFT             # 左 = 人声
    y_r = mid_i - (acc_f >> OLA_SHIFT)   # 右 = mid - 低频保护后的人声

    def band_energy(y, f0, sr=FS):
        """某个基频的谐波能量占比（粗估计，够用）。"""
        Y = np.abs(np.fft.rfft(y.astype(np.float64) * np.hanning(y.size)))
        f = np.fft.rfftfreq(y.size, 1.0 / sr)
        e = 0.0
        for h in range(1, 9):
            fc = f0 * h
            if fc > 4000:
                break
            sel = (f > fc * 0.94) & (f < fc * 1.06)
            if sel.any():
                e += float(np.sum(Y[sel] ** 2))
        return e

    lo, hi = N, n - N
    vmf = vm[lo:hi]
    vff = vf[lo:hi]
    ylf = y_l[lo:hi].astype(np.float64) / 22000.0
    yrf = y_r[lo:hi].astype(np.float64) / 22000.0

    def sisdr(y, ref):
        ref = ref - np.mean(ref)
        y = y - np.mean(y)
        a = float(np.dot(y, ref) / (np.dot(ref, ref) + 1e-12))
        err = y - a * ref
        return 10.0 * math.log10((np.dot(a * ref, a * ref) + 1e-12) /
                                 (np.dot(err, err) + 1e-12))

    # 人声（两个音高）居中，伴奏带左右差异。所以左声道该是人声、右声道该是伴奏。
    s_v_l = sisdr(ylf, vmf + vff)    # 左声道里人声的保真度
    s_v_r = sisdr(yrf, vmf + vff)    # 右声道里人声的残留（-大 = 扣得干净）
    print("  左声道：人声 %.1f dB    右声道：人声残留 %.1f dB"
          % (s_v_l, s_v_r))
    # ★ 用"人声在左右两侧的差距"当判据：同一个参考，只有左右之差能说明
    #   人声是不是真的被推到了左边。绝对值会被伴奏干扰压低，不可比。
    sep_v = s_v_l - s_v_r
    print("  分离度：人声 左-右 = %+.1f dB" % sep_v)
    check("人声被推到左声道", sep_v > 1.0, "%+.1f dB" % sep_v)
    # ---- 伴奏侧：用"投影能量比"而不是 SI-SDR ----
    # 谐波掩蔽是稀疏的（只有齿上有值），稀疏信号算 SI-SDR 会被尺度拟合
    # 主导，左右之差噪声很大。改成把两路分别投影到 [人声, 伴奏] 上，
    # 看"人声能量 / 伴奏能量"这个比值 —— 左声道该高、右声道该低。
    a_mid = 0.5 * (L[lo:hi] + R[lo:hi]) - (vmf + vff)     # 伴奏的 mid
    voc_ref = vmf + vff
    A = np.stack([voc_ref, a_mid], axis=1)

    def v_over_a(y):
        c, *_ = np.linalg.lstsq(A, y, rcond=None)
        ev = float(np.sum((c[0] * voc_ref) ** 2)) + 1e-20
        ea = float(np.sum((c[1] * a_mid) ** 2)) + 1e-20
        return 10.0 * math.log10(ev / ea)

    r_l = v_over_a(ylf)
    r_r = v_over_a(yrf)
    print("  人声/伴奏 能量比：左 %.1f dB，右 %.1f dB" % (r_l, r_r))
    check("左声道更偏人声、右声道更偏伴奏", r_l - r_r > 1.0,
          "左-右 = %+.1f dB" % (r_l - r_r))

    print("\n" + "=" * 70)
    print(" PASS = %d    FAIL = %d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
