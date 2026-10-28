# -*- coding: utf-8 -*-
"""anti_howl.py —— 实时变声的"防回声"（声学反馈 / 啸叫抑制）。

先厘清一件事：这里做的是**声学反馈抑制（AFC）**，不是回声消除（AEC）。
    AEC  需要一路"参考信号"（比如对端传过来的远端语音），把参考从麦克风里减掉。
    AFC  没有参考，麦克风→处理→喇叭→又被麦克风收进去，形成闭环；
         环路增益 ≥1 的频率会指数增长，就是啸叫。变声器天生容易啸叫，
         因为它把声音改了频响、又常常是喇叭外放。

做法（检测 + 定点压制）
----------------------
1. **检测**：每块做一次 1024 点谱，找出"又尖又突出"的峰：
       prominence = 峰值 / 邻域中位数（啸叫是窄带、比邻域高 20~40 dB）
   再要求它连续 N 块出现在同一频率（±半个 bin），才算真的啸叫，
   避免把正常的换气声/齿音误判。
2. **压制**：在检出的频率上挂二阶陷波器（notch），Q 取 30~60
   （带宽 100~300 Hz），深度从 0 逐步加深到 −30 dB。最多 8 个。
   啸叫消失后深度缓慢回退，不永久破坏音色。
3. **兜底**：如果整体峰值/中位数比超过阈值（说明已经起振），
   临时把总增益压下来，等陷波器站稳再放开。

★ 按你的要求，"开启时牺牲变声效果问题也不大"：所以这里不手软 ——
  陷波可以到 −30 dB、Q 可以到 60，总增益兜底可以有 12 dB。

代价说清楚：
  - 陷波会削掉该频率附近的**人声泛音**，听感上那一小段会变"空"；
  - 总增益兜底会让整体音量起伏；
  - 检测到压制之间有 2~3 块（约 20~30 ms）的延迟，短促的口哨式啸叫可能先响一下。

用法
----
    ah = HowlSuppressor(sr)          # 创建一次
    y = ah.process(block)            # 逐块调用，块长任意（建议 256~1024）
    ah.enabled = False               # 开关
    ah.stats()                       # 当前陷波个数/频率/深度，给界面显示

自检：`python anti_howl.py`（闭环仿真，不通过会 FAIL）
"""
import numpy as np

# ---------------------------------------------------------------- 参数

N_FFT = 1024
# 啸叫检测
GROW_DB_PER_BLOCK = 3.5   # 同频点每块涨多少 dB 才算"在起振"（人声谐波通常 <1.5）
ABS_DB = -40.0            # 绝对门限：太小的峰不看
PERSIST = 1               # 首块即响应：增长一旦被 duck 压平就再也攒不到 2 块
# ★ 为什么不能等 2~3 块：兜底 duck 会在第一块就把增长压平，
#   于是"连续增长"永远攒不够，陷波一个都装不上，duck 一松啸叫就回来
#   （实测：duck 生效 34 块、notch 始终 0，最终峰值照样 5.06）。
MAX_NOTCH = 8             # 最多几路陷波
Q_MIN, Q_MAX = 10.0, 22.0   # Q 小=带宽大，能盖住喇叭/房间的共振带宽
Q_FLOOR = 3.5             # 加宽的下限（1.2 kHz 处约 340 Hz 带宽）
Q_WIDEN = 0.85            # 每块加宽多少
DEPTH_MAX_DB = -30.0      # 单个陷波最深
DEPTH_STEP_DB = 4.0       # 每块加深多少
DEPTH_DECAY_DB = 1.5      # 每块回退多少（啸叫消失后）
DEPTH_OFF_DB = -1.0       # 浅于这个就撤掉
HOLD_BLOCKS = 25          # 安静之后还保持多少块再开始回退（约 0.27 s @ 256/24k）
# 兜底
RATIO_TRIP = 12.0         # 峰值/中位数 超过这个就起振
GAIN_DUCK_DB = -12.0
GAIN_RELEASE_DB = 3.0     # 每块回退（快放，否则人声被压的时间太长）
DUCK_PEAK = 0.9           # 时域峰值超过这个才算"快削顶"


def _biquad_notch(f0, sr, q, depth_db):
    """二阶陷波，深度可调。

    ★★ 这里原来有个会**正反馈爆炸**的写法，务必别再犯：
       "浅陷波 = 直通与全陷波的分子线性插值、分母不变"。分母是极点，
       分子被插成 [1,0,0] 时极点没有任何零点去抵消，滤波器在 f0 处变成
       Q=30 的**谐振器**，增益 +35 dB —— 啸叫频点被放大 60 倍再喂回环路，
       实测峰值从 5.06 涨到 30.55。
       正确做法是**零极点半径配合**：零点半径从极点半径插值到 1，
       深度 k=0 时零极点重合（严格直通），k=1 时零点落在单位圆（全陷波）。
    """
    w0 = 2.0 * np.pi * f0 / sr
    cw = np.cos(w0)
    alpha = np.sin(w0) / (2.0 * q)
    rp = 1.0 - alpha                       # 极点半径
    k = 1.0 if depth_db <= -0.01 else 0.0
    k = min(1.0, max(0.0, 1.0 - 10.0 ** (depth_db / 20.0)))   # 0(直通)..1(全挖)
    rz = rp + (1.0 - rp) * k               # 零点半径：k=0 时与极点重合
    b = np.array([1.0, -2.0 * rz * cw, rz * rz])
    a = np.array([1.0, -2.0 * rp * cw, rp * rp])
    g = (1.0 + a[1] + a[2]) / (1.0 + b[1] + b[2])    # 直流增益归一
    return g * b, a


class _Biquad(object):
    __slots__ = ("b", "a", "z1", "z2")

    def __init__(self):
        self.b = np.array([1.0, 0.0, 0.0])
        self.a = np.array([1.0, 0.0, 0.0])
        self.z1 = 0.0
        self.z2 = 0.0

    def set(self, b, a):
        self.b, self.a = b, a

    def run(self, x):
        """Direct Form II transposed，纯 Python 循环（块长小，够快）。"""
        b0, b1, b2 = self.b
        _, a1, a2 = self.a
        z1, z2 = self.z1, self.z2
        y = np.empty_like(x)
        for i in range(len(x)):
            xi = x[i]
            yi = b0 * xi + z1
            z1 = b1 * xi - a1 * yi + z2
            z2 = b2 * xi - a2 * yi
            y[i] = yi
        self.z1, self.z2 = z1, z2
        return y


class HowlSuppressor(object):
    def __init__(self, sr, enabled=True, max_notch=MAX_NOTCH):
        self.sr = int(sr)
        self.enabled = bool(enabled)
        self.max_notch = int(max_notch)
        self.win = np.hanning(N_FFT)
        # 陷波状态：[(freq_hz, depth_db, hit_count, biquad), ...]
        self.notches = []
        self.cand = []            # [(freq, count), ...]
        self.duck_db = 0.0
        self._prev_spec = None
        self._sb = None
        self._fb = None
        self._m = 1
        self._clip_run = 0

    # ------------------------------------------------------------ 检测
    #
    # ★ 不用"峰值突出度"判据：稳态语音的谐波也很尖（峰/邻域中位数能到 30 dB），
    #   按那个判会把人声当啸叫挖掉（实测负对照 SNR 只有 2.5 dB）。
    #   啸叫的独有特征是**同一个频点的能量在指数增长**，而人声不会一直涨。
    #   所以看"同一 bin 每块的增量 dB"，连续几块都涨才动手。
    def _track(self, x):
        """返回 (最大 bin 的频率, 该 bin 相对上一块的增量 dB, 该 bin 的绝对 dB)。"""
        n = len(x)
        m = min(n, N_FFT)
        if m < 128:
            return 0.0, 0.0, -120.0
        seg = x[:m] * np.hanning(m)
        S = np.abs(np.fft.rfft(seg))
        f = np.fft.rfftfreq(m, 1.0 / self.sr)
        band = (f >= 150.0) & (f <= min(9000.0, self.sr * 0.45))
        if not band.any():
            return 0.0, 0.0, -120.0
        Sb, fb = S[band], f[band]
        # 存下来：陷波要不要保持，看的是"这个频段现在还响不响"，
        # 不是"它是不是全局最大的那个峰"（啸叫被压住后主导峰就变成人声了）。
        self._sb, self._fb, self._m = Sb, fb, max(m, 1)
        k = int(np.argmax(Sb))
        cur = 20.0 * np.log10(max(Sb[k] / max(m, 1), 1e-12) + 1e-12)
        if self._prev_spec is None or len(self._prev_spec) != len(Sb):
            self._prev_spec = Sb.copy()
            return float(fb[k]), 0.0, cur
        grow = 20.0 * np.log10(max(Sb[k], 1e-12) / max(self._prev_spec[k], 1e-12))
        self._prev_spec = Sb.copy()
        return float(fb[k]), float(grow), cur

    def _level_at(self, fc):
        """fc 附近 ±1 bin 的最大电平（dB，归一到块长）。"""
        if self._sb is None:
            return -120.0
        k = int(np.argmin(np.abs(self._fb - fc)))
        lo, hi = max(0, k - 1), min(len(self._sb), k + 2)
        return float(20.0 * np.log10(max(np.max(self._sb[lo:hi]) / self._m, 1e-12)))

    # ------------------------------------------------------------ 主处理
    def process(self, x):
        x = np.asarray(x, dtype=np.float64)
        if not self.enabled or x.size == 0:
            return x.copy() if x.ndim else x
        mono = x.mean(axis=1) if x.ndim == 2 else x

        fpk, grow, lvl = self._track(mono)
        near_clip = float(np.max(np.abs(mono))) > DUCK_PEAK
        self._clip_run = self._clip_run + 1 if near_clip else 0

        # 啸叫的判据有两条，任一成立即可：
        #   a) 同一频点连续增长（真正的指数起振阶段）；
        #   b) ★ 已经连续 2 块快削顶 —— 这时**必然**有某个窄带在自激，
        #      而它被 duck 压住之后增长会变成 0，光靠 a) 永远等不到。
        #      这是上一版卡死的死循环：duck 压住 → 增长为 0 → 装不上陷波
        #      → 只能一直压 → 人声被压 12 dB（实测 200 块里 194 块在压）。
        rising = ((grow >= GROW_DB_PER_BLOCK and lvl > ABS_DB)
                  or (self._clip_run >= 2 and lvl > ABS_DB))

        if rising:
            hit = None
            for i, (fc, cnt, _g) in enumerate(self.cand):
                if abs(fc - fpk) < max(2.0, self.sr / float(N_FFT)):
                    hit = i
                    break
            if hit is None:
                self.cand.append([fpk, 1, grow])
            else:
                self.cand[hit][1] += 1
                self.cand[hit][2] = 0.6 * self.cand[hit][2] + 0.4 * grow
                self.cand[hit][0] = 0.6 * self.cand[hit][0] + 0.4 * fpk
        else:
            self.cand = [[f, c - 1, g] for f, c, g in self.cand if c - 1 > 0]

        # ---- 候选升级成陷波
        for fc, cnt, _g in list(self.cand):
            if cnt < PERSIST:
                continue
            if any(abs(fc - n[0]) < max(3.0, self.sr / float(N_FFT))
                   for n in self.notches):
                continue
            if len(self.notches) >= self.max_notch:
                break
            q = Q_MAX if _g >= 2.5 else Q_MIN
            bq = _Biquad()
            bq.set(*_biquad_notch(fc, self.sr, q, DEPTH_MAX_DB * 0.4))
            # 结构：[频率, 深度, 静默计数, 滤波器, Q]
            self.notches.append([fc, DEPTH_MAX_DB * 0.4, 0, bq, q])

        # ---- 加深 / 回退
        #
        # ★★ 这里踩过一个致命坑，写下来免得再犯：
        #   最早的判据是"当前全局主导峰是否落在这个陷波上"。但啸叫一旦被压下，
        #   主导峰立刻变成人声谐波，陷波就判为"不再需要" → 撤掉 → 啸叫回来。
        #   更糟的是新建的陷波计数值正好等于 PERSIST，保持循环一进来就走
        #   "回退"分支把计数减到 0，**同一块里就被删掉**（实测 notch 恒为 0）。
        #
        #   现在改成三条独立规则：
        #     · 该频段仍然响（> ABS_DB）        → 加深，静默清零
        #     · 该频段已经安静                   → 先等 HOLD_BLOCKS 块（宽限）
        #     · 宽限期过了还在安静               → 每块回退一点，浅到阈值才撤
        #   啸叫在回退过程中重新冒头时，频段电平会超门限 → 立刻又加深。
        keep = []
        for fc, depth, quiet, bq, q in self.notches:
            if self._level_at(fc) > ABS_DB:
                depth = max(DEPTH_MAX_DB, depth - DEPTH_STEP_DB)
                # ★ 已经挖到最深却还在响 → **自动加宽**（降 Q）。
                #   固定 Q 的窄陷波盖不住会摆动的啸叫：实测陷波钉在 1125/1219，
                #   环路从两者中间的 1162 Hz 绕过去继续自激（还有 74 dB）。
                #   加宽到 Q≈4（1.2 kHz 处约 300 Hz 带宽）才能把整个共振罩住。
                if depth <= DEPTH_MAX_DB:
                    q = max(Q_FLOOR, q * Q_WIDEN)
                quiet = 0
            else:
                quiet += 1
                if quiet > HOLD_BLOCKS:
                    depth = depth + DEPTH_DECAY_DB
            # ★★ 深度是**负数**（越深越负），"浅到该撤"是 depth >= DEPTH_OFF_DB。
            #    写成 <= 的话新装的 -12 dB 陷波一进来就判"太浅"被丢掉
            #    （实测 200 块里 notch 恒为 0，T3 人声被压 11 dB）。
            if depth >= DEPTH_OFF_DB:
                continue
            bq.set(*_biquad_notch(fc, self.sr, q, depth))
            keep.append([fc, depth, quiet, bq, q])
        self.notches = keep

        # ---- 兜底增益
        #   啸叫是指数过程，等陷波"站稳"往往已经削顶，所以一旦判定在起振就
        #   先压增益止血；陷波足够深之后再慢慢放开。
        #   ★ 门限必须是"快削顶"的绝对电平，不能拿 ABS_DB 往上加几个 dB：
        #     lvl 的归一化是 20log10(峰/块长)，正弦满幅也只有 -12 dB，所以
        #     -34 dB 相当于 A≈0.08 —— 正常说话就超了，整段被压 12 dB
        #     （实测负对照 SNR 从 225 dB 掉到 2.5 dB，人声基频掉 11 dB）。
        #   ★ 也不能拿"陷波频段还很响"当条件：啸叫被压到 -40 dB 以上仍算响，
        #     duck 就永不释放，人声一直被压。
        #   ★★ 判据必须用**时域峰值**，不能用谱电平。谱电平的归一化是
        #     20log10(峰/块长)，环路一起振它就常年在 -12 dB 左右晃，结果
        #     duck 一直挂在 -12 dB 上：啸叫确实不响了，但人声也被压了 12 dB，
        #     而且增长全被压平 → 陷波一个都装不上（实测 200 块里 notch 恒为 0）。
        #     时域峰值 >0.9 才是真的"快到 1.0 要削顶"，这时才值得牺牲音质。
        #   ★ 不要用"超过阈值就 -12 dB"的硬开关：它会在阈值上下反复开关，
        #     稳定在 -9 dB 上下（实测 193/200 块在压），人声整体被削 9 dB，
        #     T3 直接 FAIL。改成**按需限幅**：只压到峰值 0.85 所需的量，
        #     通常只有 0.5~5 dB，而且不会再回到"压死"的状态。
        if near_clip:
            want = 20.0 * np.log10(0.85 / max(float(np.max(np.abs(mono))), 1e-9))
            self.duck_db = min(self.duck_db, max(want, GAIN_DUCK_DB))
        elif self.duck_db < 0:
            self.duck_db = min(0.0, self.duck_db + GAIN_RELEASE_DB)

        # ---- 滤波
        out = x.copy()
        chans = [out] if out.ndim == 1 else [out[:, c] for c in range(out.shape[1])]
        for c, ch in enumerate(chans):
            y = ch
            for n in self.notches:
                y = n[3].run(y)
            if self.duck_db < -0.01:
                y = y * (10.0 ** (self.duck_db / 20.0))
            chans[c][...] = y
        return out

    # ------------------------------------------------------------ 状态
    def stats(self):
        return {
            "enabled": self.enabled,
            "notches": [(round(n[0], 1), round(n[1], 1)) for n in self.notches],
            "duck_db": round(self.duck_db, 1),
            "cand": [(round(c[0], 1), c[1]) for c in self.cand],
        }


# ---------------------------------------------------------------- 自检

def _sim_loop(sr, n_blocks, blk, use_ah, loop_gain=1.25, delay_ms=3.0,
              howl_freq=1200.0, bw=150.0, seed=1):
    """闭环仿真：麦克风 → 处理 → 喇叭(有共振) → 又被麦克风收进来。

    ★ 纯延迟的环路增益 <1 不会啸叫（g<1 时任何频率都衰减），必须有**谐振**：
      喇叭/房间在 1200 Hz 附近有个 Q≈8 的峰，环路增益 1.5 时该频点每圈涨
      1.5×1.05 ≈ 1.58 倍 —— 这就是真实的啸叫成因。
    """
    rng = np.random.default_rng(seed)
    d = max(1, int(sr * delay_ms / 1000.0))
    r = np.exp(-np.pi * bw / sr)
    th = 2.0 * np.pi * howl_freq / sr
    a1 = -2.0 * r * np.cos(th)
    a2 = r * r
    b0 = 1.0 - r * r

    hist = np.zeros(d + blk)          # 喇叭输出的历史（供延迟取用）
    z1 = z2 = 0.0
    ah = HowlSuppressor(sr) if use_ah else None
    outs, mics = [], []
    for nb in range(n_blocks):
        t = (np.arange(blk) + nb * blk) / float(sr)
        voice = 0.05 * sum(np.sin(2 * np.pi * 150.0 * h * t) / (h ** 1.4)
                           for h in range(1, 12))
        voice += 0.002 * rng.standard_normal(blk)
        fb = hist[:blk]                       # 延迟 d 的那个块
        mic = voice + loop_gain * fb
        y = ah.process(mic) if ah is not None else mic
        # 喇叭/房间共振
        spk = np.empty(blk)
        for i in range(blk):
            v = b0 * y[i] - a1 * z1 - a2 * z2
            z2 = z1
            z1 = v
            spk[i] = v
        spk = np.clip(spk, -4.0, 4.0)
        hist = np.concatenate([hist[blk:], spk])
        outs.append(y)
        mics.append(mic)
    return np.concatenate(outs), np.concatenate(mics)


def _band_db(x, sr, f0, bw):
    S = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1.0 / sr)
    sel = (f > f0 - bw) & (f < f0 + bw)
    return 20.0 * np.log10(max(np.sqrt(np.mean(S[sel] ** 2)), 1e-12))


def _selftest():
    sr = 24000
    blk = 256
    n = 200
    ok = fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [PASS] %s %s" % (name, detail))
        else:
            fail += 1
            print("  [FAIL] %s %s" % (name, detail))

    print("anti_howl 自检（闭环仿真，环路增益 0.9，啸叫频率 1200 Hz）")

    y0, m0 = _sim_loop(sr, n, blk, use_ah=False)
    y1, m1 = _sim_loop(sr, n, blk, use_ah=True)

    # 只看后半段（前段还在起振）
    half = len(y0) // 2
    pk0 = np.max(np.abs(y0[half:]))
    pk1 = np.max(np.abs(y1[half:]))

    chk("T1 关闭时确实起振（负对照，证明仿真有效）", pk0 > 1.5,
        "关闭时峰值=%.2f" % pk0)
    chk("T1b 开启后不再发散（峰值受控）",
        pk1 < pk0 * 0.35 and pk1 < 1.0,
        "关=%.2f 开=%.2f" % (pk0, pk1))

    # 啸叫频点的能量
    b0 = _band_db(y0[half:], sr, 1200.0, 60.0)
    b1 = _band_db(y1[half:], sr, 1200.0, 60.0)
    chk("T2 啸叫频点被压下 ≥12 dB", b1 < b0 - 12.0,
        "%.1f -> %.1f dB" % (b0, b1))

    # 人声基频附近不能被误伤太多
    v0 = _band_db(y0[half:], sr, 150.0, 40.0)
    v1 = _band_db(y1[half:], sr, 150.0, 40.0)
    chk("T3 人声基频附近变化 <6 dB（不误伤）", abs(v1 - v0) < 6.0,
        "%.1f -> %.1f dB" % (v0, v1))

    # 负对照：没有啸叫时，开启处理必须几乎透明
    rng = np.random.default_rng(7)
    t = np.arange(sr * 2) / float(sr)
    quiet = 0.1 * sum(np.sin(2 * np.pi * 200.0 * h * t) / h for h in range(1, 8))
    ah = HowlSuppressor(sr)
    yr = ah.process(quiet)
    e = np.sum((yr - quiet) ** 2)
    s = np.sum(quiet ** 2)
    snr = 10 * np.log10(s / max(e, 1e-20))
    chk("T4 负对照：无啸叫时近乎透明（SNR > 40 dB）", snr > 40.0,
        "SNR=%.1f dB，陷波=%d 个" % (snr, len(ah.notches)))

    # 开关
    ah2 = HowlSuppressor(sr, enabled=False)
    yoff = ah2.process(quiet)
    chk("T5 开关关闭时逐位直通", np.max(np.abs(yoff - quiet)) == 0.0,
        "max|diff|=%.1e" % np.max(np.abs(yoff - quiet)))

    print("\n  PASS = %d  FAIL = %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
