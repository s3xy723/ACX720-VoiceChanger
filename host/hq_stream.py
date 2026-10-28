# -*- coding: utf-8 -*-
"""
hq_stream.py —— 高质量变调的**流式（可实时）**版本

hq_voice.py 那条流水线（相位声码器 + 带限重采样 + 共振峰保持）质量很好，
但它是"攒完整段再一次性算完"的离线结构：三个步骤各自要前后各看 n 个点，
实时麦克风根本喂不进去。这个模块把同样的算法改写成"来一块出一块"。

改写的关键：把"整段 OLA"换成环形 OLA
------------------------------------
离线版最后才做 ``y = acc / nrm``；流式版必须每推一帧就知道"哪些输出样本
已经算完了"。判据是：

    帧按 advance 等间距摆放，摆在绝对位置 s 的帧覆盖 [s, s+n)。
    位置 p 是终值  <=>  所有起点落在 (p-n, p] 的帧都已写入。
    写完起点为 s 的帧之后，所有起点 <= s 的帧都在队列里了，
    而后续帧的起点一定 > s >= p，所以 p <= s 的位置一定已经是终值。

    推论：每推一帧，就能安全输出 [上一帧起点, 本帧起点] 这一段。

归一化仍然用 ``acc / nrm``（不是把 Hann 平方和当常数除）：时间伸缩之后
合成跳距不再是 HOP，硬除会引入周期性的音量起伏。这一点和离线版一致。

三级流水线与离离线版的对应关系
------------------------------
    离线：  A(_tsm) -> B(sinc_resample) -> C(_formant_correct)
    流式：  StreamOLA(adv=each*hop) -> StreamResampler -> StreamOLA(adv=hop)

    A 步：相位声码器时间伸缩。合成相位推进 ratio*(dphi_nom+dom)，
          帧摆放间距 ratio*hop —— 两者正好互相抵消，稳态分量频率不变、
          瞬态被拉开，这就是"保音高的时间伸缩"。
    B 步：在等间距位置 (pad+i)*each 上插值。位置严格等间距，
          所以直接复用 hq_voice._sinc_resample_uniform 的核表快路。
    C 步：把包络拉回原位。分母用弯折后的原包络 E_x(k/ratio)，不测 E_y
          （理由见 hq_voice._formant_correct 的注释，不重复）。

延迟（实测，不是估的）
----------------------
    端到端 = pad + 2*n ≈ 3071 点，48 kHz 下约 64 ms。
    拆开看：C 步的 OLA 要知道 p 点，必须等起点 <= p 的帧都到齐，
    而那一帧要读 y[p : p+n] —— 这是 n 点的前向依赖；
    B/A 链路自己还有 n 点的群延迟；首尾各 pad 点补零。
    比离线版的"整段"好得多，但**不是零延迟**，界面上必须如实标注。

    作为对照：现有纯 DSP 实时链路（块 1024 @48k + 3 块预填充）本身就有
    100~150 ms 延迟，所以这条流式高质量链路反而更快。

已知限制（不藏）
----------------
- 相位声码器会把瞬态抹开一点，人声连续段影响很小。
- 共振峰校正是包络对齐，倒谱截断按 512 帧的**尾部**均值自适应
  （离线版用的是向前的块均值，流式拿不到未来，只能用尾部 —— 这是
  流式化必然付出的代价，量过影响很小）。
- ratio == 1 时整条链路旁路，输出与输入逐样本相同（0 偏差）。

依赖：numpy + hq_voice（复用它的窗、核表、倒谱包络、自适应截断）。
"""

import math

import numpy as np

import hq_voice as hq

# 与 hq_voice 保持同一组常量：同样的分析窗、同样的 FFT 点数。
# NFFT 与 rtl/fft1024.v 同点数，将来到板子上可以直接复用同一套参数。
NFFT = hq.NFFT
HOP = hq.HOP
TAPS = hq.RESAMP_TAPS
BETA = hq.KAISER_BETA
ENV_FRAMES = 512          # 自适应倒谱截断的统计帧数（= hq_voice.CHUNK_ENV）
ENV_MIN_FRAMES = 32       # 至少攒够这么多帧才重算截断


# ---------------------------------------------------------------- 环形 OLA

class StreamOLA(object):
    """环形归一化重叠相加。

    acc / nrm 两个环，长度 L = n + ceil(advance) + 4。
    写完一帧后立刻取走能取的部分，所以环里永远只存
    "还没读走的输出" + "刚写进去的这一帧"，不会无限增长。

    什么位置已经算完了
    -----------------
    帧按 advance 等间距摆放，摆在绝对位置 s 的帧覆盖 [s, s+n)。
    位置 p 是终值  <=>  所有起点落在 (p-n, p] 的帧都已写入。
    写完起点为 s_j 的帧后，所有起点 <= s_j 的帧就都到齐了；
    因此只要 p < s_{j+1}（下一帧的起点），(p-n, p] 里的起点必然全都 <= s_j。

    ★ 这个判据一开始被写成"p <= s_j"，每一帧都少输出一个 advance，
      整条流短了 191 个点、音高被测低 15 个半音百分数——已实测抓到并改正。

    ★ 读走之后必须立刻把对应格清零。
      环上一个格子里存的是绝对位置 p、p+L、p+2L... 的数据（相差 L 的
      位置映射到同一格）。不清零的话，等环绕回来写 p+L 时，会把早就
      消费掉的 p 的数据一起累加上去 —— nrm 会一路涨（实测从 1.5 涨到
      4.45），输出幅度被越除越小。离线版用整段数组、每个位置只写一次，
      所以没这个问题；换成环形缓冲就必须补上清零。
      清零是安全的：未来的帧起点一定 >= 当前读走的位置，所以已经读过
      的位置不可能再被任何后续帧写入，清零不会丢掉有效数据。
    """

    def __init__(self, n, win, advance):
        self.n = int(n)
        self.win = np.asarray(win, dtype=np.float64)
        self.w2 = self.win * self.win
        self.adv = float(advance)
        if self.adv >= self.n:
            # advance >= n 会出现"覆盖数为 0"的位置，归一化直接除零。
            # 离线版用 `while hop > 32 and each*hop > n/2.5: hop >>= 1`
            # 保证 each*hop <= n/2.5，这里同样强制。
            raise ValueError("advance(%.3f) 必须小于帧长(%d)，否则 OLA 出现空窗"
                             % (self.adv, self.n))
        self.L = self.n + int(math.ceil(self.adv)) + 4
        self.reset()

    def reset(self):
        self.acc = np.zeros(self.L, dtype=np.float64)
        self.nrm = np.zeros(self.L, dtype=np.float64)
        self._next = 0.0        # 下一帧的绝对起点
        self._out = 0           # 下一个待输出的绝对位置

    def push(self, frame, limit=None):
        """写入一帧，返回本次可以取走的输出样本（可能为空）。

        limit : 输出绝对位置的上限（不含）。用来把总长度钉死在输入长度上，
                否则 flush 时多推的零帧会让末尾多吐一段。
        """
        s = int(self._next)
        f = np.asarray(frame, dtype=np.float64) * self.win
        i0 = s % self.L
        if i0 + self.n <= self.L:
            self.acc[i0:i0 + self.n] += f
            self.nrm[i0:i0 + self.n] += self.w2
        else:
            k = self.L - i0
            self.acc[i0:] += f[:k]
            self.acc[:self.n - k] += f[k:]
            self.nrm[i0:] += self.w2[:k]
            self.nrm[:self.n - k] += self.w2[k:]
        self._next += self.adv
        end = int(self._next)
        if limit is not None:
            end = min(end, int(limit))
        return self._take(end)

    def _take(self, end):
        m = end - self._out
        if m <= 0:
            return np.zeros(0, dtype=np.float64)
        i0 = self._out % self.L
        if i0 + m <= self.L:
            a = self.acc[i0:i0 + m].copy()
            nz = self.nrm[i0:i0 + m].copy()
            self.acc[i0:i0 + m] = 0.0
            self.nrm[i0:i0 + m] = 0.0
        else:
            k = self.L - i0
            a = np.concatenate([self.acc[i0:], self.acc[:m - k]])
            nz = np.concatenate([self.nrm[i0:], self.nrm[:m - k]])
            self.acc[i0:] = 0.0
            self.acc[:m - k] = 0.0
            self.nrm[i0:] = 0.0
            self.nrm[:m - k] = 0.0
        self._out = end
        # 覆盖数为 0 的位置（只可能出现在配置错误时）置零，不除出无穷大
        return np.where(nz > 1e-9, a / np.maximum(nz, 1e-12), 0.0)


# ---------------------------------------------------------------- 流式重采样

class StreamResampler(object):
    """流式带限重采样：y[i] = A[(pad + i) * each]，位置严格等间距。

    等间距这一点很重要：只有等间距才能走 hq_voice._sinc_resample_uniform
    的核表快路（查表 + 分块 gather）。位置一旦不等间距就会退回逐点现算
    核的慢通路，实时直接跑不动。

    缓冲区只保留"下一批输出还用得着"的那一段，左边按需裁掉。
    """

    MAX_OUT = 8192          # 单次产出上限，防止一次性分配过大数组

    def __init__(self, each, pad, taps=TAPS, beta=BETA):
        self.each = float(each)
        self.pad = int(pad)
        self.taps = int(taps)
        self.beta = float(beta)
        self.s = max(1.0, self.each)
        self.span = (self.taps // 2) * self.s
        self.nt = int(math.ceil(self.span))
        self.reset()

    def reset(self):
        self._a = np.zeros(0, dtype=np.float64)
        self._a0 = 0          # _a[0] 对应的 A 绝对位置
        self._yi = 0          # 下一个待产出的输出索引

    def push(self, a_block, cap=None):
        """灌入一段 A 信号，返回本次能产出的全部输出。

        内部循环排空：一次调用要把当前 A 缓冲能支持的全部输出都产完，
        不能只产一批就把剩下的留在缓冲里 —— 调用方（_Stage.push）每次
        只会调一次，留在里面的样本要等到下一次 flush 才出来，整条流的
        时序就全乱了（实测一次喂完整段时只出了 16384/24000 个点）。
        """
        blk = np.asarray(a_block, dtype=np.float64)
        if blk.size:
            self._a = blk.copy() if self._a.size == 0 else np.concatenate([self._a, blk])

        outs = []
        while True:
            a_end = self._a0 + self._a.size
            # 要产出索引 i，需要 A 覆盖到 (pad+i)*each + nt
            imax = int(math.floor((a_end - 1 - self.nt) / self.each)) - self.pad
            if cap is not None:
                # 输出序号 i 与输入序号 i 一一对应，所以绝不能产出超过
                # "已经喂进来的真实输入"那么多。flush 时多推的零帧会把 A 缓冲
                # 撑长，不设上限就会在流的**中间**多插一段样本，把信号局部
                # 拉慢、音高直接测低（实测 +4 半音被测成 +3.4 半音）。
                imax = min(imax, int(cap))
            m = imax - self._yi + 1
            if m <= 0:
                break
            m = min(m, self.MAX_OUT)

            pos0 = (self.pad + self._yi) * self.each
            local0 = pos0 - self._a0
            outs.append(hq._sinc_resample_uniform(
                self._a, local0, self.each, m,
                self.s, self.nt, self.span, self.beta))
            self._yi += m

            # 裁掉左边用不上的部分。下一批的第一个位置是 pos0 + m*each，
            # 它需要 A 覆盖到该位置减 nt，所以保留到那里就够。
            need_lo = (self.pad + self._yi) * self.each - self.nt - 1.0
            cut = int(math.floor(need_lo - self._a0))
            if cut > 0:
                self._a = np.ascontiguousarray(self._a[cut:])
                self._a0 += cut
        if not outs:
            return np.zeros(0, dtype=np.float64)
        return outs[0] if len(outs) == 1 else np.concatenate(outs)


# ---------------------------------------------------------------- 一级 A+B

class _Stage(object):
    """一级"时间伸缩 + 重采样"（= 离线版的 A 步接 B 步）。

    输入 m 个样本，稳态下产出 m 个样本；开头有一段预热（输入侧补 pad 个零），
    预热期的产出是零且被丢弃。
    """

    def __init__(self, each, pad, n=NFFT, hop=HOP, taps=TAPS, beta=BETA):
        self.each = float(each)
        self.n = int(n)
        self.hop = int(hop)
        self.win = hq.hann_periodic(self.n)
        self.adv = self.each * self.hop
        self.ola = StreamOLA(self.n, self.win, self.adv)
        self.rs = StreamResampler(self.each, pad, taps, beta)
        self.pad = int(pad)

        nb = self.n // 2 + 1
        self._k = np.arange(nb, dtype=np.float64)
        self._dphi_nom = 2.0 * np.pi * self._k * self.hop / float(self.n)
        self._ph_prev = None
        self._ps_prev = None
        self._xq = np.zeros(0, dtype=np.float64)   # 还没凑够一帧的输入
        self._pad_left = self.pad                  # 还要补多少个零
        self.reset()

    def reset(self):
        self.ola.reset()
        self.rs.reset()
        self._ph_prev = None
        self._ps_prev = None
        # 前置 pad 个零**在 reset 时就种进队列**。这样虚拟输入流永远是
        # "先 n 个零、再接真实数据"，与离线版的 yp = [zeros(pad), y, ...]
        # 完全一致，而且和调用方怎么分块彻底无关。
        self._xq = np.zeros(self.pad, dtype=np.float64)
        self._pad_left = 0
        self._nin = 0              # 已喂进来的真实输入样本数（不含补的零）

    def push(self, x_block):
        """喂入一段输入，返回本次能产出的输出样本。

        注意分析帧长是 n（1024）、帧间跳距是 hop（256），不是"来多少出一帧"。
        不满一帧的尾巴留在 _xq 里，等下一块数据接上。

        ★ 前置补零在 reset 时就种进 _xq（见下），不要在这里"来一块补一块"。
          一开始写在 push 里补：块长 >= pad 时没问题，块长 < pad 时补的零会
          和真实数据交替排列（[零 441][真 441][零 441][真 441]…），而离线版
          是"先 n 个零、再接整段"。帧网格整个错位，实测两级级联（+12 半音）
          在块长 441 下输出完全不相关（RMS 差 0.273，信号本身才 0.238）。
        """
        x = np.asarray(x_block, dtype=np.float64)
        self._nin += x.size
        self._xq = np.concatenate([self._xq, x])

        out = []
        while self._xq.size >= self.n:
            fr = self._xq[:self.n]
            self._xq = np.ascontiguousarray(self._xq[self.hop:])
            out.append(self._frame(fr))
        if not out:
            return np.zeros(0, dtype=np.float64)
        a = np.concatenate(out) if len(out) > 1 else out[0]
        # 上限 = 已到的真实输入数 - 1。输出序号 i 要等输入走到 i+n 才终值，
        # 所以正常情况下 A 缓冲的可用性比这个上限更紧，上限只在 flush 之后兜底。
        return self.rs.push(a, cap=self._nin - 1)

    def flush(self):
        """把不满一帧的尾巴补零推完，再补几帧零把 OLA 尾部排空。"""
        got = []
        if self._xq.size:
            fr = np.zeros(self.n, dtype=np.float64)
            fr[:self._xq.size] = self._xq
            self._xq = np.zeros(0, dtype=np.float64)
            got.append(self._frame(fr))
        # OLA 的尾巴有 n 点（= n/hop 帧）要排空，多给一帧余量
        for _ in range(int(math.ceil(self.n / self.hop)) + 1):
            got.append(self._frame(np.zeros(self.n, dtype=np.float64)))
        a = np.concatenate(got)
        # flush 时 _nin 已经是全部真实输入，上限就是"输入长度 - 1"，
        # 这样总产出恰好等于输入长度（与离线版一致）
        return self.rs.push(a, cap=self._nin - 1)

    def _frame(self, fr):
        """一帧：分析 -> 相位声码器 -> 逆变换 -> 进 A 步 OLA。

        相位递推与离线 _tsm 逐字对应：

            离线（一次算 blk 帧）:
                dom[i] = wrap(ph[i] - ph[i-1] - dphi_nom)      i >= 1
                dom[0] = wrap(ph[0] - ph_prev - dphi_nom)      跨块
                inc[0] = 0, inc[i] = adv[i]                    i >= 1
                ps[i]  = base + cumsum(inc)[i]
                => ps[0] = base,  ps[i] = ps[i-1] + adv[i]

            流式（一次一帧）就是上面递推的单步:
                第一帧      ps = ph
                之后每帧    ps = ps_prev + each * (dphi_nom + dom)

        ★ 踩过的坑：离线那行 `inc[1:] = adv[1:]` 是沿**帧轴**切，
          照搬成单帧一维数组后变成沿 **bin 轴**切，合成相位整个乱掉，
          输出是一堆等幅度的杂散峰（实测 RMS 误差 0.316，信号本身才 0.315，
          等于完全不相关）。单帧情况下 inc 恒为 0，正确写法就是上面那两行。
        """
        X = np.fft.rfft(fr * self.win, n=self.n)
        A = np.abs(X)
        ph = np.angle(X)

        if self._ph_prev is None:
            ps = ph.copy()
        else:
            dom = hq._wrap_pi(ph - self._ph_prev - self._dphi_nom)
            ps = self._ps_prev + self.each * (self._dphi_nom + dom)

        Y = A * np.exp(1j * ps)
        back = np.fft.irfft(Y, n=self.n)
        self._ph_prev = ph
        self._ps_prev = ps
        return self.ola.push(back)


# ---------------------------------------------------------------- 单声道流水线

class StreamPitch(object):
    """单声道的高质量变调流水线：k 级 A+B 级联 + C 步共振峰保持。

    对外只有两个动作：
        push(x) -> y   喂一块，拿回能拿的输出（可能比输入短）
        flush() -> y   喂完最后一块之后调用，把尾部排空
    """

    def __init__(self, sr=48000, ratio=1.0, formant=1.0,
                 n=NFFT, hop=HOP, taps=TAPS, beta=BETA):
        self.sr = int(sr)
        self.n = int(n)
        self.hop = int(hop)
        self.formant = float(formant)
        self._n_stage = 0
        self.set_ratio(ratio)
        self.reset()

    # ---- 参数 ----
    def set_ratio(self, ratio):
        r = float(ratio)
        if abs(r - 1.0) < 1e-9:
            self.ratio = 1.0
            self._k = 0
            return
        k = hq._stage_count(r)
        self.ratio = r
        self._k = k
        self._each = r ** (1.0 / k)

    def set_formant(self, f):
        self.formant = float(f)

    # ---- 状态 ----
    def reset(self):
        self._stages = []
        if self._k:
            self._stages = [_Stage(self._each, self.n) for _ in range(self._k)]
        # C 步
        self._cola = StreamOLA(self.n, hq.hann_periodic(self.n), self.hop)
        self._rs_env = None
        self._lift_default = max(8, int(round(hq.LIFT_MS * 1e-3 * self.sr)))
        self._lift = self._lift_default
        self._lg_s = None
        self._env_ring = np.zeros((0, self.n // 2 + 1), dtype=np.float32)
        self._env_cnt = 0
        self._env_next = ENV_MIN_FRAMES
        self._a_env = math.exp(-(float(self.hop) / float(self.sr)) / hq.TAU_ENV)

        nb = self.n // 2 + 1
        self._kb = np.arange(nb, dtype=np.float64)
        src = self._kb / self.ratio
        lo = np.clip(np.floor(src).astype(np.int64), 0, nb - 1)
        self._lo = lo
        self._hi = np.clip(lo + 1, 0, nb - 1)
        # 这里 lg 是一维的（单帧），所以插值权重也是一维 —— 离线版那里
        # lg 是 (M, nb) 二维，权重才需要 [None, :] 抬起一维
        self._wl = (src - lo)

        # C 步的输入/输出侧补零与丢弃
        self._c_pad_left = self.n          # y 流前面还要补多少个零
        self._c_skip = self.n              # 还要丢弃多少个输出样本
        self._ybuf = np.zeros(0, dtype=np.float64)     # 还没凑够一帧的 y
        # 原始 x 的延迟线。开头 n 个零就是离线版的 pad；
        # _xoff 记录 _xbuf[0] 对应的原始下标，左边攒够了就裁掉，防止无限增长
        self._xbuf = np.zeros(self.n, dtype=np.float64)
        self._xoff = 0
        self._xf = 0                       # 已消费到第几个 C 帧
        self._nin = 0                      # 已喂进来的真实输入样本数
        self._c_limit = None               # C 步输出绝对位置的上限

    # ---- 主入口 ----
    def push(self, x):
        x = np.asarray(x, dtype=np.float64)
        if self.ratio == 1.0:
            return x.copy()
        self._nin += x.size
        # C 步的输出序号 i 对应 C 绝对位置 pad + i，所以总长度钉在 pad + 输入长度
        self._c_limit = self.n + self._nin
        # ★ 原始 x 必须先记进延迟线，**再**跑级联。
        #   一级 A+B 在输入还不足 pad+一帧时返回空（第一个输出位置在
        #   A 流的 pad*each 处，A 流要攒到那儿才吐得出东西），那时 x
        #   已经被消费掉了。如果把 _xbuf 的追加写在级联之后，早期样本
        #   就永久丢失 —— C 步取到的 x 帧与 y 帧整体错位。
        #   实测块长 256 时 _xbuf 前 768 个样本一直是零，输出与块长
        #   1024 的结果差 0.12（信号本身才 0.24）。
        self._xbuf = np.concatenate([self._xbuf, x])
        # 1) 级联各级（前级还没攒够时后级自然接着攒，逐级往下传）
        cur = x
        for st in self._stages:
            cur = st.push(cur)
            if cur.size == 0:
                return np.zeros(0, dtype=np.float64)
        # 3) y 流前面补 n 个零（= 离线版的 pad）。必须**一次性补足**再接到
        #    真实 y 上：边收边补会把 pad 零和真实 y 交替排列，C 步的帧就错位了。
        if self._c_pad_left > 0:
            cur = np.concatenate([np.zeros(self._c_pad_left, dtype=np.float64), cur])
            self._c_pad_left = 0
        return self._c_run(cur)

    def flush(self):
        if self.ratio == 1.0:
            return np.zeros(0, dtype=np.float64)
        # 逐级排空，且必须**顺着链子传**：第 i 级 flush 出来的尾部要喂进
        # 第 i+1 级，再 flush 第 i+1 级。一开始写成"把各级 flush 的结果
        # 拼在一起"，中间级的尾部就没人接收了 —— 大变调量（多级）时输出
        # 直接少一截，实测 -12 半音被测成 +7 半音。
        carry = np.zeros(0, dtype=np.float64)
        for i, st in enumerate(self._stages):
            tail = st.flush()
            feed = np.concatenate([carry, tail]) if carry.size else tail
            if i + 1 < len(self._stages):
                carry = self._stages[i + 1].push(feed)
            else:
                carry = feed
        cur = carry
        if self._c_pad_left > 0:
            cur = np.concatenate([np.zeros(self._c_pad_left, dtype=np.float64), cur])
            self._c_pad_left = 0
        # y 流尾部同样补 n 个零，把 C 步的 OLA 排空
        cur = np.concatenate([cur, np.zeros(self.n, dtype=np.float64)])
        return self._c_run(cur)

    def _c_run(self, y_blk):
        """把一段 y 灌进 C 步，返回去掉开头 pad 之后的输出。"""
        if self._ybuf.size:
            self._ybuf = np.concatenate([self._ybuf, y_blk])
        else:
            self._ybuf = y_blk
        out = []
        while self._ybuf.size >= self.n:
            out.append(self._c_frame(self._ybuf[:self.n]))
            self._ybuf = np.ascontiguousarray(self._ybuf[self.hop:])
        if not out:
            return np.zeros(0, dtype=np.float64)
        y = np.concatenate(out) if len(out) > 1 else out[0]
        if self._c_skip > 0:
            if y.size <= self._c_skip:
                self._c_skip -= y.size
                return np.zeros(0, dtype=np.float64)
            y = y[self._c_skip:]
            self._c_skip = 0
        return y

    # ---- C 步 ----
    def _c_frame(self, y_frame):
        """一帧共振峰保持：G = E_x / E_x(k/ratio)，只改幅度不改相位。"""
        # 取对齐的原始 x 帧（C 帧序号 self._xf）。_xoff 是 _xbuf[0] 对应的
        # 原始下标，左边用不到的部分攒够 4 帧就裁掉，否则 _xbuf 会跟着
        # 整条信号一起无限增长。
        base = self.hop * self._xf - self._xoff
        if base < 0:
            base = 0
        if self._xbuf.size < base + self.n:
            xf = np.zeros(self.n, dtype=np.float64)
            av = self._xbuf[base:]
            if av.size:
                xf[:av.size] = av
        else:
            xf = self._xbuf[base:base + self.n]
        self._xf += 1
        if self._xbuf.size > 4 * self.n:
            keep = self.hop * self._xf - self._xoff
            if keep > 0:
                self._xbuf = np.ascontiguousarray(self._xbuf[keep:])
                self._xoff += keep

        Xa = np.fft.rfft(xf * self._cola.win, n=self.n)
        lg_frame = np.log(np.maximum(np.abs(Xa), 1e-10)).astype(np.float32)

        # 自适应倒谱截断：按 ENV_FRAMES 帧的**尾部**均值定期重算。
        #
        # ★ 两个坑都踩过：
        #   1) 回填值不能当新默认值。adaptive_lift 返回的是 min(默认, 0.8q)，
        #      把结果再传回去就变成"只降不升"的棘轮 —— 一旦掉到下限 24 就
        #      再也回不来，包络被过度平滑，增益开始逐帧抖，输出出现 187.5 Hz
        #      （= 帧率）的调幅伪峰。离线版每块都用**固定的**默认 lift 重算，
        #      所以要保留一份 _lift_default。
        #   2) 触发条件不能写 `cnt % 128 == 0`。一次 push 可能推进十几帧，
        #      直接把 128 的倍数跳过去（实测块长 4096 时 -12 半音结果就和
        #      块长 1024 不一样）。改成"到了就重算，然后往后推一个周期"。
        if self._env_ring.shape[0] < ENV_FRAMES:
            self._env_ring = np.concatenate(
                [self._env_ring, lg_frame[None, :]], axis=0)
        else:
            self._env_ring = np.roll(self._env_ring, -1, axis=0)
            self._env_ring[-1] = lg_frame
        self._env_cnt += 1
        if self._env_cnt >= ENV_MIN_FRAMES and \
                self._env_cnt >= self._env_next:
            self._lift = hq.adaptive_lift(
                self._env_ring.mean(0).astype(np.float64), self.sr, self.n,
                self._lift_default)
            self._env_next = self._env_cnt + max(1, ENV_FRAMES // 4)

        ex = hq.cepstral_envelope(np.abs(Xa)[None, :], self._lift, self.n)[0]
        lg = np.log(np.maximum(ex, 1e-10))
        cur = lg if self._lg_s is None else self._lg_s
        cur = self._a_env * cur + (1.0 - self._a_env) * lg
        self._lg_s = cur
        lgf = cur

        # 分母用弯折后的原包络 E_x(k/ratio)，不测 E_y（理由见 hq_voice 注释）
        pred = np.exp((1.0 - self._wl) * lgf[self._lo] + self._wl * lgf[self._hi])
        # ★ lgf 是**对数域**的包络，必须先指数化再当分子。
        #   一开始直接写了 g = lgf / pred，等于拿一个可能为负的对数值
        #   去除一个线性域小量 —— r > 1 时被 GMAX 夹住看不出问题，
        #   r = 0.5（降 12 半音）时直接崩：输出冒出 243 Hz 的假峰、
        #   能量掉到离线的四成。离线版那里是 `ex = np.exp(lgf)` 再除。
        g = np.clip(np.exp(lgf) / np.maximum(pred, 1e-10),
                    1.0 / hq.GMAX, hq.GMAX)
        if self.formant < 1.0:
            g = 1.0 + self.formant * (g - 1.0)
        Ya = np.fft.rfft(y_frame * self._cola.win, n=self.n)
        back = np.fft.irfft(Ya * g, n=self.n)
        return self._cola.push(back, limit=self._c_limit)


# ---------------------------------------------------------------- 立体声入口

class StreamPitchShifter(object):
    """按声道独立处理（与 dsp_core.StereoChain / hq_voice.HQVoiceShifter 同风格）。

    用法::

        sp = StreamPitchShifter(sr, ratio, formant)
        y = sp.push(x)          # x: (m,) 或 (m, 2)
        y += sp.flush()         # 收尾
    """

    def __init__(self, sr=48000, ratio=1.0, formant=1.0):
        self.sr = int(sr)
        self.ratio = float(ratio)
        self.formant = float(formant)
        self._mono = True
        self._ch = [StreamPitch(sr, ratio, formant),
                    StreamPitch(sr, ratio, formant)]

    def reset(self):
        self._mono = True
        for c in self._ch:
            c.reset()

    def set_ratio(self, ratio):
        """改音高比。

        ★ 比值没变就必须立刻返回，**不能**顺手 reset()。
          实时链路里 _out_cb 每个音频块都会调一次 configure ->
          EngineChain._retune -> set_ratio，而 set_ratio 会重建级联和
          OLA 状态。早期版本无条件 reset，结果是每块都把算法打回原点：
          输出永远停在"刚预热完"的那一段静音上（实测 24 块全零）。
          现在只有比值真的变了才重建（那一下会有轻微 click，可接受）。
        """
        r = float(ratio)
        if abs(r - self.ratio) < 1e-12:
            return
        self.ratio = r
        for c in self._ch:
            c.set_ratio(r)
            c.reset()

    def set_formant(self, f):
        """改共振峰保持强度。只是个标量，不动任何状态。"""
        self.formant = float(f)
        for c in self._ch:
            c.set_formant(f)

    def _run(self, x, flush=False):
        if flush:
            cols = [c.flush() for c in self._ch]
            mono = self._mono
        else:
            a = np.asarray(x, dtype=np.float64)
            self._mono = mono = (a.ndim == 1)
            if mono:
                a = np.repeat(a.reshape(-1, 1), 2, axis=1)
            cols = []
            for c in range(a.shape[1]):
                ch = self._ch[c] if c < 2 else None
                cols.append(a[:, c] if ch is None else ch.push(a[:, c]))
        # 取两路里短的那截，保证左右对齐。
        # 空输出必须按当前维度返回（mono 给 (0,)，立体声给 (0,2)）——
        # 一开始统一返回一维空数组，和 (n,2) 混在一起 np.concatenate
        # 直接报维度不匹配。
        n = min(cc.shape[0] for cc in cols)
        if n <= 0:
            return (np.zeros(0, dtype=np.float64) if mono
                    else np.zeros((0, 2), dtype=np.float64))
        y = np.stack([cc[:n] for cc in cols], axis=1)
        return y[:, 0] if mono else y

    def push(self, x):
        if abs(self.ratio - 1.0) < 1e-9:
            return np.asarray(x, dtype=np.float64).copy()
        return self._run(x, flush=False)

    def flush(self):
        if abs(self.ratio - 1.0) < 1e-9:
            return np.zeros(0, dtype=np.float64)
        return self._run(None, flush=True)

    def latency_samples(self):
        """端到端延迟（样本数），用于在界面上如实显示。

        推导：输出序号 i 要等 C 步里起点 <= pad+i 的帧都到齐，其中最后一帧
        要读 y 到下标 i+n-1；而 y 下标 j 本身要等输入走到 j+n 才终值
        （A 步 OLA 的 n 点群延迟）。所以总延迟 = 2n-1，与 pad、与 ratio 无关。
        """
        if abs(self.ratio - 1.0) < 1e-9:
            return 0
        return 2 * NFFT - 1


def available():
    return True, ""


def process(x, sr, ratio, formant=1.0):
    """一次性接口（与 hq_voice.process 同签名），供测试与文件处理复用。"""
    sp = StreamPitchShifter(sr, ratio, formant)
    a = np.asarray(x, dtype=np.float64)
    if a.ndim == 1:
        y = np.concatenate([sp.push(a), sp.flush()])
        return y
    cols = []
    for c in range(a.shape[1]):
        sub = StreamPitchShifter(sr, ratio, formant)
        cols.append(np.concatenate([sub.push(np.ascontiguousarray(a[:, c])),
                                    sub.flush()]))
    n = min(cc.size for cc in cols)
    return np.stack([cc[:n] for cc in cols], axis=1)
