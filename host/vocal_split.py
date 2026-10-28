# -*- coding: utf-8 -*-
"""
vocal_split.py —— 中置分离 + 左右声道分配（纯 DSP，不需要模型 / 新依赖）

唯一的输出形式（2026-10-21 起）
------------------------------
    **左声道 = 人声，右声道 = 伴奏。**

    这里只做"人声 / 伴奏"这一件事。旧的 route="mf"（男声→左、女声→右）
    已经整条删掉 —— 需求从一开始就只是把人和伴奏分开，性别不是目的。
    两个音高候选（f1/f2）仍然保留，但那只是为了在男女对唱时把**两把梳子
    并起来**覆盖住两个歌手的谐波，跟"谁男谁女"无关，也不再往不同声道送。

    人声用中置提取 + 谐波梳；从伴奏里扣人声时走「伴奏低频保护」高通，
    保住贝斯/底鼓（实测值 +9.3 dB）。

思路（四步，全部在 int16 之外用 float64 做）
--------------------------------------------
1. 中置提取（center extraction）

   立体声混音里人声几乎总是居中（左右两声道几乎相同），伴奏则带有左右差异。
   每帧做短时傅里叶变换，用 mid / side 的幅度比算"中置度"：

       mid = (L + R) / 2      side = (L - R) / 2
       c   = clip((|mid| - |side|) / |mid|, 0, 1)

   c≈1 表示这个频点在两声道里一模一样（人声、贝斯、底鼓…），
   c≈0 表示这个频点只存在于某一侧（立体声伴奏）。

2. 谐波梳状掩蔽

   只靠中置度会把"居中但一直在响"的贝斯 / 底鼓也当成歌声一起搬走。
   所以再用上一帧跟踪到的基频 F0 生成一条谐波梳，只保留 F0 各次谐波的
   频点；非谐波的中置成分只留 `broadband` 那一份（齿音、气声靠它保住）。

3. 逐帧基频 -> 男女判定

   对提取出的人声做 60~1200 Hz 带通，归一化自相关找第一个显著峰，
   得到该帧 F0 与浊音置信度。F0 序列先做 9 帧中值滤波压掉颤音和倍频跳变，
   再用一个滑动窗（约 6 秒）里的浊音 F0 做两类聚类，两簇中心的中点
   就是这首歌当下的男女分界；聚类分不开时回退到 165 Hz。
   判定带 6% 迟滞，另外有 150 ms 的 hangover —— 音节之间的空隙不会
   让人声一下子弹回中间。

4. 声像重定位（注意：不是"把伴奏也搬走"）

       out_L = L - aL * v
       out_R = R - aR * v

   v 是从中置成分里提出来的**人声估计**，aL / aR 是"从该声道扣除多少"：

       男声帧  (aL, aR) = (0, 1)   人声留在 L，从 R 里扣掉
       女声帧  (aL, aR) = (1, 0)   人声留在 R，从 L 里扣掉
       未判定帧(aL, aR) = (0, 0)   原样直通

   因为 v 本来就是"两声道共有的那部分"，从另一侧扣掉它以后，
   **伴奏（= 左右各自减去 v 剩下的部分）在两个声道里一点没动**，
   人声则只出现在一侧。判定翻转时有 30 ms 线性交叉淡化。

   流式实现：帧网格锚定在绝对样本下标 k*HOP 上，与调用方怎么分块无关。
   延迟 = NFFT = 1024 点（48 kHz 下 21 ms），输出内容与输入逐点对齐
   （out[p] 对应 in[p]），只是要先攒够 NFFT 点才能算出来。

已知限制（写在 README 里，不藏）
--------------------------------
- 中置提取假设人声居中。如果原曲人声本来就被摆在一侧，效果会明显变差。
- 男女同时唱的合唱段落里，一帧只有一个 F0，会按占优的那一方走，
  属于本方法的原理性限制（要真正分开需要多 F0 估计或神经网络）。
- 单声道文件没有 side 信息，中置度恒为 1，只能退化成纯谐波提取，
  效果会差很多（代码里会自动识别并降级，界面上也有提示）。
"""

import numpy as np

# ---------------------------------------------------------------- 常量

NFFT = 1024          # STFT 点数：48 kHz 下 21 ms，频率分辨率 46.9 Hz
HOP = 256            # 帧步进：75% 重叠，Hann 窗 WOLA 重构增益 = 2/3
F0_WIN = 2048        # 基频估计窗：42.7 ms，70 Hz 有 3 个周期
LONG_SEC = 6.0       # 聚类用的滑动窗长度
HIST_FRAMES = 9      # F0 中值滤波长度
LONG_MAX = int(LONG_SEC * 48000.0 / HOP)   # 上面按 48 kHz 算，换采样率会重算

# ---- 双音高（两位歌手）估计 ----
F0_LO = 70.0         # 候选基频下限
F0_HI = 500.0        # 候选基频上限
F0_CAND = 96         # 候选基频个数（对数均分）
F0_BAND = 0.045      # 每个谐波取多宽的带（相对频率），至少 28 Hz
F0_HARM_MAX = 4500.0  # 谐波累加到多高为止
F0_SPLIT = 165.0     # 男/女音域分界（自动模式会据此调整）
F0_REL_SAFE = 0.055  # 两个音高若成整数倍（相差 < 5.5%）就当成同一个人的谐波
F0_PRESENCE = 0.45   # 次要歌手至少要有主要歌手这么多谐波支撑才算"在唱"
# 八度折叠容差：两个基频候选成 2:1 到这个程度，就认定是同一个人的两个倍频
# （自相关在 2×f0 处同样有峰）。必须与 vocal_route.OCT_TOL 取同一个值，
# 否则文件模式和实时模式的判定会不一致。
OCT_TOL = 0.06
WHITEN_HZ = 220.0    # 谱白化的滑动平均宽度

# (aL, aR)：从左右声道各扣除多少比例的 v
KEEP_MALE = (0.0, 1.0)       # 男声：留在 L，从 R 扣除
KEEP_FEMALE = (1.0, 0.0)     # 女声：留在 R，从 L 扣除
KEEP_NONE = (0.0, 0.0)       # 未判定：原样


def _periodic_hann(n):
    """周期 Hann（分母是 n 不是 n-1），保证 WOLA 的互补关系严格成立。"""
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / float(n))).astype(np.float64)


_WIN = _periodic_hann(NFFT)
_WIN_F0 = _periodic_hann(F0_WIN)
_OLA = 2.0 / 3.0             # hop = NFFT/4 时 Σw² = 1.5


def _kmeans2(v):
    """一维两类聚类，返回排序后的两个簇中心。"""
    v = np.asarray(v, dtype=np.float64)
    if v.size < 2:
        return float(v[0]) if v.size else 0.0, float(v[0]) if v.size else 0.0
    c0, c1 = float(v.min()), float(v.max())
    if c1 - c0 < 1e-9:
        return c0, c1
    for _ in range(30):
        mid = 0.5 * (c0 + c1)
        a = v[v < mid]
        b = v[v >= mid]
        if a.size == 0 or b.size == 0:
            break
        n0, n1 = float(a.mean()), float(b.mean())
        if abs(n0 - c0) < 1e-9 and abs(n1 - c1) < 1e-9:
            c0, c1 = n0, n1
            break
        c0, c1 = n0, n1
    return (c0, c1) if c0 <= c1 else (c1, c0)


# ---------------------------------------------------------------- 主体

def extract_center(x, sr, gate_db=-35.0, strength=1.0, floor=0.0):
    """把立体声混音里的"中置成分"整段提出来，返回单声道 (n,) float32。

    这是纯 DSP 的人声前端（人声 + 居中乐器都在里面）。
    现在整条链路是两段式：

        extract_center / demucs  →  负责"把人声提出来"
        vocal_route.route_vocal  →  负责"按男女分到左右"

    分成两段的好处：装了 demucs 就把它的 vocals 轨直接换进来，
    后半段一行都不用改。
    """
    a = np.asarray(x, dtype=np.float32)
    if a.ndim == 1:
        a = np.repeat(a.reshape(-1, 1), 2, axis=1)
    L = a[:, 0].astype(np.float64)
    R = a[:, 1].astype(np.float64)
    n = len(L)
    out = np.zeros(n)
    cnt = np.zeros(n)
    nfr = max(0, (n - NFFT) // HOP + 1)
    for k in range(nfr):
        s = k * HOP
        ls = L[s:s + NFFT] * _WIN
        rs = R[s:s + NFFT] * _WIN
        M = np.fft.rfft(0.5 * (ls + rs))
        Sd = np.fft.rfft(0.5 * (ls - rs))
        m = np.abs(M)
        sd = np.abs(Sd)
        c = np.clip((m - sd) / (m + 1e-12), 0.0, 1.0)
        if strength != 1.0:
            c = c ** strength
        if floor > 0.0:
            c = floor + (1.0 - floor) * c
        if gate_db < 0.0 and m.size:
            gth = float(m.max()) * (10.0 ** (gate_db / 20.0))
            if gth > 0.0:
                c = c * np.clip((m - gth) / gth, 0.0, 1.0)
        # ★ 这里原来写成 `* _WIN * _OLA` 再在下面 `out /= cnt`，把 WOLA 的
        #   归一化做了两遍：Σw² 稳态是 1.5，_OLA 又是 1/1.5，于是纯居中
        #   信号被固定衰减到 0.667（-3.5 dB）。人声本来就是居中的，等于
        #   每一次分离都先把人声削掉三分之一 —— 实测男声轨幅度只有输入的
        #   64%，其中这一条就占了大头。
        #   除以 cnt 已经是完整的 WOLA 归一化，这里不能再乘 _OLA。
        out[s:s + NFFT] += np.fft.irfft(c * M, NFFT) * _WIN
        cnt[s:s + NFFT] += _WIN * _WIN
    out /= np.maximum(cnt, 1e-9)
    return out.astype(np.float32)


def _route_acf(seg, sr, fmin, fmax, whiten_hz=0.0, whiten_cap=4.0):
    """借 vocal_route 的自相关基频估计（两处必须用同一套，否则实时和文件不一致）。"""
    from vocal_route import _acf_f0
    return _acf_f0(seg, sr, fmin, fmax, whiten_hz=whiten_hz,
                   whiten_cap=whiten_cap)


class VocalSplitter(object):
    """立体声 -> 男声左 / 女声右，伴奏不动。

    典型用法（文件模式，整段）::

        sp = VocalSplitter(sr)
        sp.configure(depth=1.0)
        y = sp.process(x)          # x: (n,) 或 (n,2)，返回同样形状

    实时模式用 :meth:`push`，每次返回"本次能算出来的"样本，长度不固定。
    """

    def __init__(self, sr=48000):
        self.sr = int(sr)
        self._freqs = np.fft.rfftfreq(NFFT, 1.0 / self.sr)
        self.configure()
        self.reset()

    # ------------------------------------------------------------ 参数

    def configure(self, depth=1.0, floor=0.06, strength=1.0, broadband=0.20,
                  harmonic=True, f0_hz=0.0, f0_min=70.0, f0_max=500.0,
                  swap=False, xfade_ms=30.0, hang_ms=150.0, conf_min=0.35,
                  gate_db=-40.0, bass_protect_hz=120.0,
                  comb_bw_min=18.0, comb_bw_rel=0.01, comb_fmax=11000.0,
                  comb_harm_max=48, f0_whiten_hz=250.0, f0_whiten_cap=8.0,
                  sub_widen=2.5, sub_broadband=0.20, sub_dilate=2):
        """
        depth      分离深度 0..1（1 = 完全只留一侧；0.5 = 另一侧只扣一半）
        floor      中置掩蔽下限 0..0.9（越大，未分离的残留越多，越安全）
        strength   中置度取幂，>1 更激进
        broadband  非谐波中置成分保留比例 0..1（齿音/气声靠它）
        harmonic   是否启用谐波梳状掩蔽
        f0_hz      两个音高候选的音域分界（Hz），0 = 自动聚类。
                   只影响"往哪个区间去找第二条基频"，与性别无关。
        swap       True = 人声去右声道、伴奏去左声道（默认人声在左）
        gate_db    相对本帧中置谱峰值的幅度门限（dB，负数）。低于它的频点
                   一律不参与提取 —— 中置度是个"比值"判据，在只有泄漏的
                   频点上比值毫无意义，会把硬声像伴奏的旁瓣也当成中置成分。
        """
        self.depth = float(np.clip(depth, 0.0, 1.0))
        self.floor = float(np.clip(floor, 0.0, 0.9))
        self.strength = max(0.1, float(strength))
        self.broadband = float(np.clip(broadband, 0.0, 1.0))
        self.harmonic = bool(harmonic)
        self.f0_hz = float(f0_hz)
        self.f0_min = float(f0_min)
        self.f0_max = float(f0_max)
        self.swap = bool(swap)
        self.conf_min = float(conf_min)
        self.gate_db = float(gate_db)
        self.bass_protect_hz = float(max(0.0, bass_protect_hz))
        # 谐波梳齿宽（见 _comb 的说明）。默认值与改版前完全一致，
        # 所以不传这几个参数的旧行为不变。
        self.comb_bw_min = float(max(1.0, comb_bw_min))
        self.comb_bw_rel = float(max(0.0, comb_bw_rel))
        self.comb_fmax = float(max(100.0, comb_fmax))
        self.comb_harm_max = int(max(1, comb_harm_max))
        # 基频分析窗的谱白化宽度（Hz），0 = 关闭。见 vocal_route._acf_f0。
        self.f0_whiten_hz = float(max(0.0, f0_whiten_hz))
        self.f0_whiten_cap = float(max(1.0, f0_whiten_cap))
        # 扣伴奏用的宽梳倍率 / 宽带保留比例（见 _do_frame 里的说明）
        # sub_dilate > 0 时优先用"频点膨胀"（快）；= 0 才退回"重算宽梳"（慢但更准）
        self.sub_widen = float(max(0.1, sub_widen))
        self.sub_broadband = float(np.clip(sub_broadband, 0.0, 1.0))
        self.sub_dilate = int(max(0, min(8, sub_dilate)))
        self._xfade = max(1, int(round(xfade_ms * 1e-3 * self.sr)))
        self._hang = max(0, int(round(hang_ms * 1e-3 * self.sr / HOP)))
        self._long_max = max(60, int(round(LONG_SEC * self.sr / HOP)))
        self._build_hp()

    def reset(self):
        """清空全部流式状态（换文件 / 重新开始实时时必须调用）。"""
        self._mix = None          # 还没被帧消费掉的输入，(m,2)
        self._mix_base = 0        # self._mix[0] 对应的绝对样本下标
        self._vv = None           # 人声（中置 + 谐波掩蔽）累加器 -> 左声道
        self._vs = None           # 从伴奏里扣除的人声（低频保护后）
        self._n_added = 0         # 累计喂入的输入样本数
        self._n_frames = 0        # 已处理的帧数
        self._n_out = 0           # 已产出的输出样本数
        self._f0_hist = []
        self._f0_long = []
        self._hold = 0
        self._thr = float(F0_SPLIT)
        self._mono = False
        self._mono_votes = 0
        self._mono_frames = 0
        self._km_tick = 0
        self._peak = 0.0
        # ---- 音轨导出（可选：人声 / 伴奏）----
        self._stems_on = False
        self._chunk_v = []        # 人声（单声道）
        self._chunk_o = []        # 伴奏（单声道）
        self.n_voiced = 0         # 检出至少一个音高的帧数
        self.n_dual = 0           # 同时检出两个音高的帧数
        self.n_unvoiced = 0       # 一个音高都没检出的帧数
        self.n_vocal = 0
        self.f0_now = 0.0
        self.conf_now = 0.0
        self._build_combs()

    # ------------------------------------------------------------ 流式接口

    def push(self, chunk):
        """喂一段 (m,1/2) 或 (m,) 的样本，返回本次能算出的输出（可能少于 m）。"""
        chunk = np.asarray(chunk, dtype=np.float64)
        if chunk.ndim == 1:
            chunk = np.repeat(chunk.reshape(-1, 1), 2, axis=1)
        if chunk.shape[1] == 1:
            chunk = np.repeat(chunk, 2, axis=1)
        elif chunk.shape[1] > 2:
            chunk = chunk[:, :2]
        m = chunk.shape[0]
        if m <= 0:
            return np.zeros((0, 2), dtype=np.float64)

        if self._mix is None:
            self._mix = chunk.copy()
        else:
            self._mix = np.concatenate([self._mix, chunk], axis=0)
        self._n_added += m

        # 帧 k 需要输入到 k*HOP + NFFT
        while self._n_frames * HOP + NFFT <= self._n_added:
            k = self._n_frames
            s = k * HOP - self._mix_base
            self._do_frame(k, self._mix[s:s + NFFT])
            self._n_frames += 1

        lim = int(min(self._n_frames * HOP, self._n_added))
        out = self._emit(lim)
        # 保留 F0 分析窗所需的历史（F0_WIN - HOP 点），其余裁掉
        keep = max(0, lim - (F0_WIN - HOP))
        self._trim(keep)
        return out

    def flush(self, upto=None, max_rounds=8):
        """补零把尾部帧推完。upto 给定时会一直补到输出样本数 >= upto。

        注意不能只补一轮 NFFT：帧网格锚定在 k*HOP 上，输入长度不是 HOP 的
        整数倍时，最后一小段输出要靠更多的补零帧才能算完，否则尾部会被
        当成"没输出"而丢掉（表现为结尾几十毫秒被静音顶替）。
        """
        out = [self.push(np.zeros((NFFT, 2), dtype=np.float64))]
        if upto is not None:
            r = 0
            while self._n_out < int(upto) and r < max_rounds:
                out.append(self.push(np.zeros((NFFT, 2), dtype=np.float64)))
                r += 1
        return np.concatenate(out, axis=0)

    def process(self, x, block=8192, stems=False, progress=None):
        """整段处理，输出与输入等长、逐点对齐（无前导静音，尾部不丢）。

        输出**永远**是 (n, 2)：这个模式的产物本身就要分到左右两声道，
        不存在"单声道输出"这种形态。单声道输入会先复制成两路。

        stems=True 时返回 ``(y, stems_dict)``，stems_dict 含三轨：
            male    (n,)   判给男声的那部分人声（单声道）
            female  (n,)   判给女声的那部分人声（单声道）
            accomp  (n,2)  去掉中间人声之后的伴奏（立体声）
        男声/女声/伴奏三轨相加 ≈ 原混音（差的那点是"未判定为人声"的部分）。

        progress: 可选的 ``fn(done, total)``，用于界面进度条。
        """
        # ★ 整段处理必须从干净状态开始。帧网格 / _n_added / _n_out /
        # _mix_base 都是跨调用连续的，同一实例第二次 process() 时
        # _mix_base 已经不是 0，_emit 会从新缓冲区的中间开始取，
        # 输出整体错位、尾部补零。dsp_core.StereoChain 复用同一个
        # VocalSplitter 实例，只要被调用两次就会踩到。
        self.reset()
        a = np.asarray(x)
        # int16 / int32 / uint8 都算整型：原来只判 == np.int16，
        # 来一份 int32 音频会被当成 [-1,1] 浮点，clip 后削成方波。
        src = np.asarray(x)
        if src.ndim > 2:                    # (n, c, 1) 之类，先压掉多余维
            src = src.reshape(src.shape[0], -1)
        was_int = np.issubdtype(src.dtype, np.integer)
        f = src.astype(np.float64)
        if was_int:
            if src.dtype == np.uint8:
                f = (f - 128.0) / 128.0     # 8-bit WAV 无符号、128 是零点
            else:
                info = np.iinfo(src.dtype)
                f = f / float(max(abs(info.min), info.max))
        if f.ndim == 1:
            f = np.repeat(f.reshape(-1, 1), 2, axis=1)
        elif f.shape[1] == 1:
            f = np.repeat(f, 2, axis=1)
        elif f.shape[1] > 2:
            f = f[:, :2]

        n = f.shape[0]
        want_stems = bool(stems)
        if want_stems:
            self._stems_on = True
            self._chunk_v, self._chunk_o = [], []

        outs = []
        p = 0
        try:
            while p < n:
                take = min(int(block), n - p)
                outs.append(self.push(f[p:p + take]))
                p += take
                if progress is not None:
                    progress(p, n)
            outs.append(self.flush(upto=n))
            y = np.concatenate(outs, axis=0) if outs else np.zeros((0, 2))
            if y.shape[0] < n:                  # 理论上不会发生，兜底
                y = np.concatenate([y, np.zeros((n - y.shape[0], 2))], axis=0)
            y = y[:n]
        finally:
            got_stems = None
            if want_stems:
                got_stems = self._collect_stems(n)
                self._stems_on = False

        # 边缘淡化：只缩放"扣除量"(y - f)，不影响直通部分。
        # ★ 现在的输出是"左人声 / 右伴奏"，已经不是"原信号 + 扣除量"的
        #   结构，套这个淡化会把两路互相渗透。首尾的 OLA 淡入淡出由
        #   Hann 窗自身给出（帧网格锚定在 k*HOP 上），不需要再补一层。

        if was_int:
            out = np.clip(np.round(y * 32767.0), -32768, 32767).astype(np.int16)
            if got_stems is not None:
                got_stems = self._stems_to_int16(got_stems)
        else:
            out = y.astype(np.float32)
            if got_stems is not None:
                got_stems = {k: v.astype(np.float32) for k, v in got_stems.items()}
        return (out, got_stems) if want_stems else out

    def _collect_stems(self, n):
        def cat1(chunks):
            return (np.concatenate(chunks, axis=0)[:n] if chunks
                    else np.zeros(0, dtype=np.float64))

        return dict(vocal=cat1(self._chunk_v), accomp=cat1(self._chunk_o))

    @staticmethod
    def _stems_to_int16(st):
        out = {}
        for k, v in st.items():
            out[k] = np.clip(np.round(v * 32767.0), -32768, 32767).astype(np.int16)
        return out

    # ------------------------------------------------------------ 缓冲区

    def _grow(self, end_abs):
        need = end_abs - self._mix_base
        if need <= 0:
            return
        if self._vv is None:
            self._vv = np.zeros(need, dtype=np.float64)
            self._vs = np.zeros(need, dtype=np.float64)
            return
        if self._vv.shape[0] < need:
            pad = need - self._vv.shape[0]
            self._vv = np.concatenate([self._vv, np.zeros(pad)])
            self._vs = np.concatenate([self._vs, np.zeros(pad)])

    def _trim(self, keep_abs):
        cut = keep_abs - self._mix_base
        if cut <= 0:
            return
        self._mix = self._mix[cut:]
        if self._vv is not None:
            self._vv = self._vv[cut:]
            self._vs = self._vs[cut:]
        self._mix_base = keep_abs

    def _emit(self, lim):
        a = self._n_out - self._mix_base
        b = lim - self._mix_base
        if b <= a or self._vv is None:
            self._n_out = max(self._n_out, lim)
            return np.zeros((0, 2), dtype=np.float64)
        seg = self._mix[a:b]
        # 左 = 人声（中置 + 谐波掩蔽），右 = 伴奏（左右相加的一半
        # 再减去人声）。两路都是单声道内容，各自占一个物理声道。
        # ★ 扣伴奏用的是**低频保护后**的人声，贝斯/底鼓不会被一起
        #   扣掉（见 _build_hp 的说明）。
        v = self._vv[a:b]
        mid = 0.5 * (seg[:, 0] + seg[:, 1])
        acc = mid - self._vs[a:b]
        y = np.empty_like(seg)
        if self.swap:
            y[:, 0] = acc
            y[:, 1] = v
        else:
            y[:, 0] = v
            y[:, 1] = acc
        if self._stems_on:
            self._chunk_v.append(v.copy())
            self._chunk_o.append(acc.copy())
        self._n_out = lim
        pk = float(np.max(np.abs(y))) if y.size else 0.0
        if pk > self._peak:
            self._peak = pk
        return y

    # ------------------------------------------------------------ 单帧

    def _do_frame(self, k, seg):
        seg = np.asarray(seg, dtype=np.float64)
        L = np.fft.rfft(seg[:, 0] * _WIN)
        R = np.fft.rfft(seg[:, 1] * _WIN)
        mid = 0.5 * (L + R)
        side = 0.5 * (L - R)
        m = np.abs(mid)
        s = np.abs(side)

        # ---- 单声道识别：前 20 帧投票，side 能量几乎为 0 就认定是单声道源
        if self._mono_frames < 20:
            self._mono_frames += 1
            if float(np.sum(s)) < 0.02 * float(np.sum(m)) + 1e-12:
                self._mono_votes += 1
            if self._mono_frames == 20:
                self._mono = self._mono_votes >= 16

        # ---- 中置掩蔽（人声 = 左右共有的那部分）
        if self._mono:
            base = np.ones_like(m)                      # 没有 side，只能靠谐波梳
        else:
            base = np.clip((m - s) / (m + 1e-12), 0.0, 1.0)
            if self.strength != 1.0:
                base = base ** self.strength
            if self.floor > 0.0:
                base = self.floor + (1.0 - self.floor) * base
        # 幅度门限：中置度只是比值，在没有能量的泄漏频点上比值没有意义
        if self.gate_db < 0.0 and m.size:
            gth = float(m.max()) * (10.0 ** (self.gate_db / 20.0))
            if gth > 0.0:
                base = base * np.clip((m - gth) / gth, 0.0, 1.0)

        V = base * mid                                  # 保留相位

        # ---- 双音高估计：一帧里最多找两位歌手，各自建一把谐波梳
        #      基频用**自相关**（和 vocal_route 同一套），不用谐波积。
        #      真人谐波能量集中在共振峰区、基波往往很弱，谐波积会被倍频盖过去；
        #      自相关看的是波形周期重复性，不依赖基波强弱。
        #      而且只依赖当前帧，不依赖上一帧的掩蔽结果，
        #      否则"错的梳子 → 错的基频 → 更错的梳子"会自己咬自己。
        mag = np.abs(V)
        end = k * HOP + HOP - self._mix_base
        st0 = end - F0_WIN
        if st0 >= 0:
            fw = 0.5 * (self._mix[st0:end, 0] + self._mix[st0:end, 1])
        else:
            fw = np.concatenate([np.zeros(-st0),
                                 0.5 * (self._mix[0:end, 0] + self._mix[0:end, 1])])
        split = self._thr
        wz = self.f0_whiten_hz
        wc = self.f0_whiten_cap
        f1_raw, s1 = _route_acf(fw, self.sr, self.f0_min, split * 1.06, wz, wc)
        f2_raw, s2 = _route_acf(fw, self.sr, split * 0.94, self.f0_max, wz, wc)
        f1_raw, s1, f2_raw, s2, H1, H2 = self._residual_test(
            mag, f1_raw, s1, f2_raw, s2)
        self._f0_hist.append((f1_raw, f2_raw))
        if len(self._f0_hist) > HIST_FRAMES:
            self._f0_hist.pop(0)
        f1 = self._med([x[0] for x in self._f0_hist])
        f2 = self._med([x[1] for x in self._f0_hist])
        # ---- 八度折叠：与 vocal_route.route_vocal 完全同一套规则 ----
        # 自相关在 2×f0 处同样有峰，独唱女声 231 Hz 会在男声区被报成
        # 115.8 Hz，于是被当成"男女齐唱"、能量对半分；反过来独唱男声
        # 110 Hz 又会在女声区报出 220 Hz。所以必须按"低候选处有没有谱峰"
        # 来二选一，不能只看显著度。文件模式与实时模式共用同一份实现。
        from vocal_route import resolve_octave
        f1, f2 = resolve_octave(f1, f2, m, self.sr)
        # 中值滤波改了基频就得重建梳子；没有平滑结果就退回本帧原始的
        if abs(f1 - f1_raw) > 1e-6:
            H1 = self._comb(f1) if f1 > 0 else None
        if abs(f2 - f2_raw) > 1e-6:
            H2 = self._comb(f2) if f2 > 0 else None
        self.f0_now = f1 if f1 > 0 else f2
        self.conf_now = min(1.0, max(s1, s2))

        if f1 > 0 or f2 > 0:
            self._f0_long.append(f1 if f1 > 0 else f2)
            if len(self._f0_long) > self._long_max:
                self._f0_long.pop(0)
            self._hold = self._hang
        elif self._hold > 0:
            self._hold -= 1
        self._km_tick += 1
        if self._km_tick >= 25:
            self._km_tick = 0
            self._update_threshold()

        # ---- 谐波竞争掩蔽：每个频点按"哪把梳子解释得更好"分给两个人
        mk1, mk2 = self._compete(f1, f2, H1, H2, s1, s2, m)

        # ---- route="va"：人声 = 两把梳子合起来的中置成分 ----
        # 谐波掩蔽只管"人声的谐波齿"，齿与齿之间（气声、齿音、唇齿噪声）
        # 全丢掉会听成"电话里的机器人"，所以再保留 broadband 这一份
        # 非谐波中置成分。0% = 最干净但发闷，25% 是实测甜点。
        # ★ 必须在下面"深度"缩放**之前**算，否则 depth 会被乘两次。
        mask_v = np.clip(mk1 + mk2, 0.0, 1.0)
        if self.harmonic:
            mask_v = mask_v + self.broadband * (1.0 - mask_v)
        else:
            mask_v = np.clip(mask_v, 0.0, 1.0)
        if self.depth < 1.0:
            mask_v = mask_v * self.depth

        # ---- 扣伴奏用的**另一把**掩蔽：齿更宽 ----
        # 精校实测：人声通道锐化到 18 Hz 齿之后，人声 SIR 从 +0.33 升到
        # +1.83，但伴奏 SDR 从 +6.19 掉到 +5.03 —— 因为扣得更少，人声在
        # 伴奏里留得更多。两者用的是同一个 mask_v，天生互相拉扯。
        # 所以扣伴奏单独算一遍，用宽的梳子 + 纯谐波（不加 broadband）。
        # 实现上优先用**频点膨胀**而不是重算一把宽梳：膨胀只要几次
        # np.maximum，重算要多跑两遍 _comb + _compete，实测慢 30%
        # （5.3× → 3.7× 实时）。1 个 bin = 23.8 Hz @24.4 kHz，
        # 膨胀 1 格 ≈ 齿半宽 18+23.8 = 41.8 Hz，接近 widen=2.5 的 45 Hz。
        mask_vs = mask_v
        if True:                      # 只有一种路由：左人声 / 右伴奏
            if self.sub_dilate > 0:
                mv = np.clip(mk1 + mk2, 0.0, 1.0)
                # ★ 变量名不能用 k：_do_frame 的帧号参数就叫 k，会被覆盖，
                #   于是下面 self._grow(k*HOP+NFFT) 和 n0 = k*HOP-... 全错，
                #   表现为 _vm 永远只长到 1280、OLA 写到固定位置，
                #   最后 _emit 里 mid - self._vs[a:b] 形状对不上。
                for _d in range(1, self.sub_dilate + 1):
                    mv = np.maximum(mv, np.roll(mv, _d))
                    mv = np.maximum(mv, np.roll(mv, -_d))
                mask_vs = mv
                if self.harmonic:
                    mask_vs = mask_vs + self.sub_broadband * (1.0 - mask_vs)
                if self.depth < 1.0:
                    mask_vs = mask_vs * self.depth
            elif self.sub_widen != 1.0:
                Hm_s = self._comb(f1, self.sub_widen) if f1 > 0 else None
                Hf_s = self._comb(f2, self.sub_widen) if f2 > 0 else None
                mm_s, mf_s = self._compete(f1, f2, Hm_s, Hf_s, s1, s2, m)
                mask_vs = np.clip(mm_s + mf_s, 0.0, 1.0)
                if self.harmonic:
                    mask_vs = mask_vs + self.sub_broadband * (1.0 - mask_vs)
                if self.depth < 1.0:
                    mask_vs = mask_vs * self.depth

        n0 = k * HOP - self._mix_base
        self._grow(k * HOP + NFFT)
        # 只累加两条轨：人声（给左声道）和"要扣的那个人声"（低频保护后）。
        self._vv[n0:n0 + NFFT] += np.fft.irfft(mask_v * V, NFFT) * _WIN * _OLA
        self._vs[n0:n0 + NFFT] += np.fft.irfft(mask_vs * self._hp * V,
                                               NFFT) * _WIN * _OLA

        # ---- 观测/统计（界面用）
        if f1 > 0 or f2 > 0:
            self.n_vocal += 1
            self.n_voiced += 1
            if f1 > 0 and f2 > 0:
                self.n_dual += 1
        else:
            self.n_unvoiced += 1

    @staticmethod
    def _med(vals):
        vals = [v for v in vals if v > 0]
        return float(np.median(vals)) if len(vals) >= 3 else 0.0

    # ------------------------------------------------------------ 估计

    def _build_hp(self):
        """伴奏低频保护的一阶高通幅频响应（作用在"要扣掉的人声"上）。

        为什么需要它：中置提取把**所有**居中成分都算进人声，包括贝斯和
        底鼓。如果拿完整人声估计去减伴奏，贝斯/底鼓也会被一起扣掉 ——
        实测合成混音上伴奏 SI-SDR 从 +1.6 dB 掉到 -2.5 dB，听感就是
        "伴奏突然变薄、没低音"。

        解法：人声输出仍然用完整掩蔽（低频男声不能丢），但**从伴奏里
        扣除的那一份**先过高通，120 Hz 以下基本不扣。代价是低频人声会
        在伴奏里留一点（实测泄漏 -14 dB），换来伴奏的低音完整（+9.7 dB）。
        0 = 关闭保护（回到"全频段扣干净"），200~300 Hz = 更保守。
        """
        fc = float(getattr(self, "bass_protect_hz", 0.0))
        if fc <= 0.0:
            self._hp = np.ones_like(self._freqs)
        else:
            f = np.maximum(self._freqs, 1.0)
            self._hp = 1.0 / (1.0 + (fc / f) ** 2)

    def _build_combs(self):
        """预算好"候选基频 × 频点"的谐波采样矩阵。

        用**谐波积**（几何平均）而不是谐波和：
            某个候选在实际谐波处都取到峰 → 积很大；
            候选是真实基频的 2 倍 → 它有一半谐波落在峰与峰之间，积立刻塌掉；
            候选是真实基频的一半 → 它多出来的那些"谐波"落在空处，积也塌掉。
        谐波**和**区分不了这两种差八度的情况（空处贡献 0，和几乎不变），
        这正是第一版把 118 Hz 的男声报成 236 Hz、又把 232 Hz 的女声
        报成 116 Hz 的原因。
        """
        f = self._freqs
        cands = np.geomspace(F0_LO, F0_HI, F0_CAND)
        M = np.zeros((len(cands), f.size), dtype=np.float32)
        for ci, f0 in enumerate(cands):
            h = 1
            while h * f0 <= F0_HARM_MAX:
                fc = float(h * f0)
                j = int(np.searchsorted(f, fc))
                j = min(max(j, 0), f.size - 1)
                M[ci, j] += 1.0 / h          # 1/h 权重：低次谐波更可信
                h += 1
        self._cands = cands
        self._combPt = M
        self._male_idx = np.where(cands < F0_SPLIT)[0]
        self._fem_idx = np.where(cands >= F0_SPLIT)[0]
        # 谱白化用的滑动平均核（宽度约 220 Hz）
        w = max(3, int(round(WHITEN_HZ / (self.sr / float(NFFT)))))
        self._whiten_k = np.ones(w, dtype=np.float64) / w

    def _salience(self, mag):
        """谐波积打分（在对数域做加权和 = 几何平均）。"""
        # 先做 3 点局部极大，让每个谐波落在最近的 bin 上也能取到峰值
        pk = np.maximum(np.maximum(mag, np.roll(mag, 1)), np.roll(mag, -1))
        L = np.log(self._whiten(pk) + 1e-6)
        return self._combPt @ L


    def _pick_two(self, mag):
        """在男 / 女两个音域里各取最强候选，再判断第二位到底是不是**另一个人**。

        判据不是"两个基频是不是整数倍"——男声 118 Hz、女声 236 Hz 这种
        **男女差八度**恰恰是最常见的合唱写法，用整数倍去挡会把真人挡掉。
        改用**残差解释力**：

            先用男声这把梳子把频谱能覆盖的部分去掉，
            看女声那把梳子还能在残差里解释多少能量。
            如果几乎解释不了新东西 → 所谓"女声基频"只是男声的谐波，同一个人。

        这样"八度齐唱"和"单人唱"都会正确地只算一个人 ——
        代价是八度齐唱时两人本来就分不开（谐波位置完全重合），
        属于这类方法的物理极限，不是实现问题。
        """
        mw = self._whiten(mag)
        sal = self._salience(mag)
        mi, fi = self._male_idx, self._fem_idx
        im = int(mi[np.argmax(sal[mi])])
        i_f = int(fi[np.argmax(sal[fi])])
        f1 = float(self._cands[im])
        f2 = float(self._cands[i_f])
        s1 = float(sal[im])
        s2 = float(sal[i_f])

        H1 = self._comb(f1)
        H2 = self._comb(f2)
        Em = float((H1 * mag).sum())
        Ef = float((H2 * mag).sum())
        # 对方的梳子在"自己解释不了的那部分"里还能捞到多少
        res_m = mag * (1.0 - np.minimum(1.0, H1))
        res_f = mag * (1.0 - np.minimum(1.0, H2))
        Ef_new = float((H2 * res_m).sum())
        Em_new = float((H1 * res_f).sum())

        both = (Ef_new > F0_PRESENCE * Em) and (Em_new > F0_PRESENCE * Ef)
        if both:
            return f1, s1, f2, s2, H1, H2
        # 只有一个人：选**谐波积更高**的那个，不是能量更高的那个。
        # 女声唱 232 Hz 时，候选 116 的梳子把她所有谐波都盖住了，
        # 两者能量几乎相等；只有谐波积能分出谁是真基频。
        if s1 >= s2:
            return f1, s1, 0.0, 0.0, H1, None
        return 0.0, 0.0, f2, s2, None, H2

    def _whiten(self, mag):
        """谱白化：除掉平滑包络，让谐波峰从伴奏的宽带背景里凸显出来。

        不做这一步的话，低频的贝斯 / 底鼓会把整个候选网格压过去，
        基频估计会被伴奏牵着走。
        """
        s1 = np.convolve(mag, self._whiten_k, mode="same")
        return mag / (s1 + 1e-9)

    def _compete(self, f1, f2, H1, H2, s1, s2, m):
        """两把梳子的竞争归一化：每个频点按"谁解释得更好"分给两个歌手。

        从 _do_frame 里原样抽出来的（2026-10-21），因为现在要算**两遍**：
        一遍给窄梳（人声输出），一遍给宽梳（扣伴奏）。抽出来之前这段
        逻辑在 _do_frame 里，复制一份必然两边不同步。
        """
        if H1 is None and f1 > 0:
            H1 = self._comb(f1)
        if H2 is None and f2 > 0:
            H2 = self._comb(f2)
        if not self.harmonic:       # 关掉梳子：按活跃度做宽带切分
            H1 = np.full_like(m, 1.0) if f1 > 0 else np.zeros_like(m)
            H2 = np.full_like(m, 1.0) if f2 > 0 else np.zeros_like(m)
        if H1 is None:
            H1 = np.zeros_like(m)
        if H2 is None:
            H2 = np.zeros_like(m)
        tot = H1 + H2
        mk1 = np.where(tot > 1e-9, H1 / (tot + 1e-9), 0.0)
        mk2 = np.where(tot > 1e-9, H2 / (tot + 1e-9), 0.0)
        # 5 kHz 以上的齿音/气声不进梳子，按两位歌手的谐波支撑比例分
        hi = np.clip((self._freqs - 5000.0) / 3000.0, 0.0, 1.0) * 0.6
        if f1 > 0 and f2 > 0:
            share = s1 / (s1 + s2 + 1e-9)
            mk1 = mk1 * (1.0 - hi) + hi * share
            mk2 = mk2 * (1.0 - hi) + hi * (1.0 - share)
        elif f1 > 0:
            mk1 = mk1 * (1.0 - hi) + hi
        elif f2 > 0:
            mk2 = mk2 * (1.0 - hi) + hi
        else:
            mk1 = np.zeros_like(m)
            mk2 = np.zeros_like(m)
        return mk1, mk2

    def _comb(self, f0, widen=1.0):
        """单个基频的谐波梳（平顶三角齿 + 高频齿音通道）。

        widen > 1 时齿宽按比例放大 —— 2026-10-21 精校新增：**人声输出**
        和**从伴奏里扣掉的人声**要用两把不同的梳子。人声通道要窄（干净），
        扣伴奏要宽（扣干净，宁可多扣一点），否则锐化人声必然让伴奏变脏。

        齿宽由 `comb_bw_min` / `comb_bw_rel` 决定（2026-10-21 从硬编码
        35 Hz / 3% 改成可调 —— 用真实歌曲精校时发现这是最大的单一杠杆：
        原来每根齿的全宽是 2×70 = 140 Hz，而 fs=24.4 kHz 时相邻谐波
        间隔只有 f0（人声 150~400 Hz），齿宽接近齿距，选择性基本没有）。
        """
        f = self._freqs
        hm = np.zeros_like(f)
        if f0 <= 0:
            return hm
        w = float(widen) if widen and widen > 0 else 1.0
        bw_min = float(getattr(self, "comb_bw_min", 35.0)) * w
        bw_rel = float(getattr(self, "comb_bw_rel", 0.03)) * w
        # ★ 齿宽不能窄于一个 DFT bin。NFFT=1024 时 bin 间距 = sr/1024：
        #   24.4 kHz 下 23.8 Hz、48 kHz 下 46.9 Hz。18 Hz 的齿在 48 kHz 下
        #   连一个 bin 都盖不住，一个纯音的能量会被相邻 bin 分走一大半，
        #   实测 231 Hz 的幅度只剩 73%。下限取 0.6 个 bin（三角齿的半宽，
        #   全宽 1.2 bin），既保住选择性又不至于切掉峰。
        bw_min = max(bw_min, 0.6 * float(self.sr) / float(NFFT))
        fmax = float(getattr(self, "comb_fmax", 11000.0))
        hmax = int(getattr(self, "comb_harm_max", 48))
        h = 1
        while h * f0 < fmax and h <= hmax:
            fc = float(h * f0)
            bw = max(bw_min, fc * bw_rel)
            np.maximum(hm, np.clip(2.0 - np.abs(f - fc) / bw, 0.0, 1.0), out=hm)
            h += 1
        return hm

    def _residual_test(self, mag, f1, s1, f2, s2):
        """两位歌手判定：看第二位的梳子在第一位解释不了的残差里还能捞到多少。

        不用"是否整数倍"去挡 —— 男 118 Hz + 女 236 Hz 这种**差八度齐唱**
        恰好是最常见的写法，挡掉就把真人挡没了。
        严格的八度重合（谁都不解释新东西）按"两人都在唱"处理，
        能量对半分，好过整段推错边。
        """
        H1 = self._comb(f1) if f1 > 0 else None
        H2 = self._comb(f2) if f2 > 0 else None
        if H1 is None or H2 is None:
            if f1 > 0:
                return f1, s1, 0.0, 0.0, H1, None
            if f2 > 0:
                return 0.0, 0.0, f2, s2, None, H2
            return 0.0, 0.0, 0.0, 0.0, None, None
        Em = float((H1 * mag).sum())
        Ef = float((H2 * mag).sum())
        r_m = mag * (1.0 - np.minimum(1.0, H1))
        r_f = mag * (1.0 - np.minimum(1.0, H2))
        Ef_new = float((H2 * r_m).sum())
        Em_new = float((H1 * r_f).sum())
        ratio = max(f1, f2) / max(min(f1, f2), 1e-9)
        nr = round(ratio)
        octave_locked = (nr >= 2 and abs(ratio - nr) / nr < 0.03)
        both = (Ef_new > 0.45 * Em and Em_new > 0.45 * Ef) or octave_locked
        if both:
            return f1, s1, f2, s2, H1, H2
        # 只有一个人：选谐波积/自相关打分更高的那个，不是能量更高的那个
        if s1 >= s2:
            return f1, s1, 0.0, 0.0, H1, None
        return 0.0, 0.0, f2, s2, None, H2

    def _update_threshold(self):
        """用观测到的两个歌手基频分布，自动调整男 / 女音域分界。

        注意这里只影响"两把梳子各自在哪个音域里找峰"，不再决定某帧归谁——
        归属是由谐波竞争逐频点决定的。所以分界偏一点也不会像以前那样
        让整帧跳到另一边。
        """
        if self.f0_hz > 0.0:
            self._thr = float(self.f0_hz)
            self._rebuild_ranges()
            return
        if len(self._f0_long) < 40:
            return
        v = np.log2(np.asarray(self._f0_long, dtype=np.float64))
        c0, c1 = _kmeans2(v)
        if (c1 - c0) < 0.17:               # 两簇分不开（只有一种性别）
            return
        t = 2.0 ** (0.5 * (c0 + c1))
        self._thr = float(min(max(t, 130.0), 240.0))
        self._rebuild_ranges()

    def _rebuild_ranges(self):
        """男 / 女音域**故意留重叠**：分界附近的声音两边都找一遍，
        免得一位歌手唱到边界时整个人消失。"""
        c = self._cands
        self._male_idx = np.where(c < self._thr * 1.25)[0]
        self._fem_idx = np.where(c >= self._thr * 0.75)[0]

    # ------------------------------------------------------------ 观测

    def summary(self):
        tot = self.n_voiced + self.n_unvoiced
        pc = (lambda x: 0.0) if tot <= 0 else (lambda x: 100.0 * x / tot)
        return dict(
            frames=tot,
            voiced_pct=pc(self.n_voiced),
            dual_pct=pc(self.n_dual),          # 两个音高同时在场的帧（对唱）
            unvoiced_pct=pc(self.n_unvoiced),
            vocal_pct=pc(self.n_vocal),
            thr_hz=self._thr,
            auto=(self.f0_hz <= 0.0),
            mono=self._mono,
            f0=self.f0_now,
            conf=self.conf_now,
            peak=self._peak,
        )

