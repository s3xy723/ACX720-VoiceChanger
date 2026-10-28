# -*- coding: utf-8 -*-
"""
local_stream.py —— 本地 AI 模型（demucs）的**流式**人声分离

这个模块解决什么事
------------------
local_ai.run() 是离线整段的：要把整首歌喂进去才能开始算，延迟按分钟算。
实时变声要的是"麦克风说一句话，音箱立刻出声"，所以需要把 demucs
切成块流着跑。

但这里有一件事必须说清楚，不能骗人：

    ★ htdemucs 的训练分段长度是 7.8 s。切得比它小，模型看到的上下文就
      比训练时短，分离质量会掉；块越小延迟越低，质量越差。

所以本模块把三个量都做成可配置、并且**如实显示**：

    block_s   每块的长度（秒）。块攒满才推理，是延迟的主要来源。
    ctx_s     每块前后多带的上下文（秒）。只用于喂模型，不进输出。
              没有它，块边界处模型"看不到前文"，人声会出现
              周期性的呼吸声/撕裂感。
    fade_s    相邻两块输出的交叉淡化长度（秒）。掩盖块边界的
              微小不连续。

    端到端延迟 ≈ block_s + fade_s + 单块推理耗时

本地实测（RTX 5070 Ti，block 2.0 s + ctx 2.0 s）：单块推理约 0.15 s，
端到端约 2.4 s。这个数字会原样显示在界面上，不会写成"实时"。

关于降级
--------
torch / demucs 不在时就跑。此时本模块退化为**直通**（输入原样输出），
并在 info 里写明"已降级为直通，原因：缺少 xxx"。绝不静默返回错结果，
也绝不因为缺依赖而崩。
"""

import threading

import numpy as np

import local_ai


# 默认值。ctx=2.0 s 是实测甜点（合成歌曲上 vocals SI-SDR）：
#     ctx=1.0 -> 15.80 dB    ctx=2.0 -> 18.37 dB    ctx=3.0 -> 14.79 dB
# 离线整段参考是 18.44 dB，所以 ctx=2 时流式只差 0.07 dB。
# blk 影响的是延迟而不是质量（blk=1/2/3 都在 18.3 dB 左右），取 1 s 压延迟。
# 单块推理实测 0.055 s（RTX 5070 Ti），远小于块长，实时可行。
DEFAULT_BLOCK_S = 1.0
DEFAULT_CTX_S = 2.0
DEFAULT_FADE_S = 0.25

# 实时链路固定用哪一档质量。
#
# ★ 这是实测出来的，不是拍脑袋：块 1 s、ctx 2 s，同一段 20 s 真实歌曲
#   （RTX 5070 Ti）——
#       fast (shifts=0)  RTF 0.22   与离线差 −25.7 dB   包络相关 0.998
#       std  (shifts=2)  RTF 0.77   与离线差 −27.7 dB   包络相关 0.999
#   std 只多 2 dB，却慢 3.5 倍。0.77 意味着 1 秒音频要 0.77 秒算，
#   只剩 23% 余量 —— 声卡回调、界面重绘、别的线程一抖就欠载，
#   表现是周期性卡顿/吞音。那种断续听起来就是"分离得脏"。
#   所以实时恒用 fast：先把 100% 算得完放在前面，质量留给文件模式
#   （文件模式走 local_ai.separate 整段推理，可以随便开 high）。
REALTIME_QUALITY = local_ai.QUALITY_FAST


class StreamSeparator(object):
    """流式 demucs 分离：push 一段立体声，返回已能取走的 (人声, 伴奏)。

    输入/输出都是 (n, 2) float32，采样率一致，输出与输入**逐样本对齐**
    （第 i 个输入样本对应第 i 个输出样本，不含延迟补偿；延迟是真实的，
    只是不额外插样本）。
    """

    def __init__(self, sr, device="auto", quality=local_ai.QUALITY_STD,
                 block_s=DEFAULT_BLOCK_S, ctx_s=DEFAULT_CTX_S,
                 fade_s=DEFAULT_FADE_S, progress=None):
        self.sr = int(sr)
        self.device = device
        # 界面上的质量档是给**文件模式**（整段推理）用的；实时这条链路
        # 每块只有 1 s 的墙钟预算，std/high 必然欠载（见 REALTIME_QUALITY
        # 上面的实测数字）。所以这里统一降到 fast，并把降级如实写进 info，
        # 不让用户以为实时也吃到了 high 档。
        self.quality_wanted = quality
        self.quality = REALTIME_QUALITY
        self._quality_downgraded = (quality != REALTIME_QUALITY)
        self.block = max(0.25, float(block_s))
        self.ctx = max(0.0, float(ctx_s))
        self.fade = min(float(fade_s), self.block * 0.5)
        self._progress = progress or (lambda s: None)

        self._blk = int(round(self.block * self.sr))
        self._ctx = int(round(self.ctx * self.sr))
        self._fade = int(round(self.fade * self.sr))
        # cross-fade 的步进：每块只产 blk-fade，起点每次前进这么多
        self._hop = max(1, self._blk - self._fade)

        # ---- 环境自检：不可用就降级为直通 ----
        ok, why = local_ai.demucs_ready()
        self.available = ok
        self.why = why
        self._degraded = not ok

        # ---- 流状态 ----
        # _in      输入缓冲，_in[0] 的全局序号是 _in_base
        # _out_pos 下一个待产出的全局序号（产出与输入逐样本对齐）
        # 不变式：_in_base <= _out_pos - _ctx（缓冲开头始终留着前文上下文）
        self._in = np.zeros((0, 2), dtype=np.float64)
        self._in_base = 0
        self._out_pos = 0
        self._voc_tail = None       # 上一块尾部 _fade 个样本（待淡化重叠）
        self._acc_tail = None
        self._closed = False
        self._lock = threading.Lock()
        self.info = {"engine": [], "degraded": self._degraded}
        if self._degraded:
            self.info["engine"].append("人声分离：降级为直通（%s）" % why)
        else:
            self.info["engine"].append(
                "人声分离：demucs 流式（块 %.2f s + 上下文 %.2f s + 淡化 %.2f s，"
                "%s 档）" % (self.block, self.ctx, self.fade, self.quality))
            if self._quality_downgraded:
                self.info["engine"].append(
                    "质量档由「%s」降为「%s」：实时每块只有 %.1f s 预算，"
                    "更高档会欠载掉帧（文件模式仍用你选的那一档）"
                    % (self.quality_wanted, self.quality, self.block))

    # ------------------------------------------------------------ 对外

    def latency_s(self):
        """端到端延迟（秒）。

        = block + ctx。ctx 是"后文上下文"：推起点为 s 的块需要数据已经
        到 s+blk+ctx，所以最后一个样本要多等 ctx 秒。fade 不额外增加
        延迟（它是块间重叠，不是等待）。单块推理耗时与硬件相关，另算。
        """
        return self.block + self.ctx

    def push(self, x):
        """喂入一段音频，返回 (voc, acc)，可能为空数组。"""
        with self._lock:
            return self._push(np.asarray(x, dtype=np.float64))

    def flush(self):
        """输入结束，把剩下的块全部挤出来。"""
        with self._lock:
            return self._push(np.zeros((0, 2), dtype=np.float64), final=True)

    # ------------------------------------------------------------ 内部

    def _push(self, a, final=False):
        if a.ndim == 1:
            a = np.repeat(a.reshape(-1, 1), 2, axis=1)
        if a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        if a.size:
            self._in = (np.concatenate([self._in, a]) if self._in.size else a)

        if self._degraded:
            # 直通：输入原样返回，伴奏给静音（与 local_ai 的"未启用"一致）
            j0 = self._out_pos - self._in_base
            voc = self._in[j0:] if j0 > 0 else self._in
            n_out = voc.shape[0]
            self._out_pos += n_out
            self._in = np.zeros((0, 2), dtype=np.float64)
            # ★ 清空之后 _in[0] 就是"下一个全局位置"，所以 _in_base 要
            #   跟上新的 _out_pos。写成 `_in_base = 旧 _out_pos` 会让下次
            #   j0 = n_out != 0，而 _in 里只有新数据，于是每隔一次 push
            #   就返回空（实测 44.1k 输入只吐出 23.6k）。
            self._in_base = self._out_pos
            return (voc.astype(np.float32),
                    np.zeros((n_out, 2), dtype=np.float32))

        outs_v = []
        outs_a = []
        if final:
            # ---- flush：把剩余的真实输入一次推完 ----
            # push 阶段只要 avail >= hop+blk+ctx 就推，所以剩下的真实输入
            # 一定少于 hop+blk+ctx，补零凑够"一块 + 后文上下文"即可。
            real = self._in.shape[0] - (self._out_pos - self._in_base)
            if real > 0:
                pad = max(0, self._blk - real) + self._ctx
                self._in = np.concatenate(
                    [self._in, np.zeros((pad, 2), dtype=np.float64)])
                blk = self._emit_block(real, final=True)
                if blk is not None:
                    v, a2 = blk
                    outs_v.append(v)
                    outs_a.append(a2)
            # 上一块留下待淡化的尾巴，后面没有块了，直接产出
            if self._voc_tail is not None:
                outs_v.append(self._voc_tail)
                outs_a.append(self._acc_tail)
                self._voc_tail = None
                self._acc_tail = None
            self._closed = True
        else:
            # ---- 常规：够一块就推 ----
            # 第一块前面没有前文可看（lo 被 clamp 到 0），只要 blk+ctx 就够；
            # 后续块有不变式 _in_base <= _out_pos - ctx 兜底，前文已经在
            # 缓冲里。写成 blk+2*ctx 会白等一个 ctx 才出第一个样本。
            while self._in.shape[0] - (self._out_pos - self._in_base) >= \
                    self._blk + self._ctx:
                blk = self._emit_block(self._blk, final=False)
                if blk is None:
                    break
                v, a2 = blk
                outs_v.append(v)
                outs_a.append(a2)

        if not outs_v:
            return (np.zeros((0, 2), dtype=np.float32),
                    np.zeros((0, 2), dtype=np.float32))
        voc = np.concatenate(outs_v) if len(outs_v) > 1 else outs_v[0]
        acc = np.concatenate(outs_a) if len(outs_a) > 1 else outs_a[0]
        return (voc.astype(np.float32), acc.astype(np.float32))

    def _emit_block(self, take, final):
        """把全局区间 [self._out_pos, self._out_pos+take) 推成输出。

        实际喂给模型的区间是 [start-ctx, start+take+ctx)，只保留中间
        take 个样本的输出；相邻两块在 fade 长度上交叉淡化，所以
        **非末块只产出 take-fade 个样本**，尾部 fade 个留给下一块。

        这是标准的 cross-fade 几何：块起点 0, hop, 2*hop, ...，
        hop = blk - fade。

        ★ 缓冲索引必须用 `lo - self._in_base`，不是 `lo - self._out_pos`。
          一开始写成了后者：ctx>0 时它恒为负，numpy 的负索引从**尾部**
          取数，喂给模型的 chunk 整个是错乱的 —— 表现为流式 SI-SDR
          只有 -7 dB，而离线整段是 +18 dB。改用 _in_base 后正确。
        """
        start_glob = self._out_pos
        lo = max(0, start_glob - self._ctx)
        need_hi = start_glob + take + self._ctx
        have_hi = self._in_base + self._in.shape[0]
        if have_hi < need_hi:
            # 后文上下文不够：只有 flush 补过零才可能到这里
            if not self._closed:
                return None

        i0 = lo - self._in_base
        i1 = min(self._in.shape[0], need_hi - self._in_base)
        if i1 <= i0:
            return None
        chunk = np.ascontiguousarray(self._in[i0:i1])

        # ---- 推理（复用 local_ai.separate：内部做重采样/归一化/apply_model）----
        try:
            voc_c, acc_c, sep = local_ai.separate(
                chunk.astype(np.float32), self.sr,
                device=self.device, quality=self.quality)
        except Exception as e:
            # 推理失败不能静默：标记降级 + 直通本块，让人在日志里看见
            self._degraded = True
            self.available = False
            self.why = "推理失败：%s" % e
            self.info["engine"].append("人声分离：推理失败，降级为直通（%s）" % e)
            step = take if final else self._hop
            j0 = start_glob - self._in_base
            # ★ 缓冲不够时必须补零到 step，不能直接返回短数组。
            #   原来这里把短数组返回却照样 _advance(step)，
            #   "产出长度 < 推进长度"会让整条流从这一块起永久错位，
            #   后面每个块边界都累积偏差（表现为周期性吞音/重复）。
            v = np.zeros((step, 2), dtype=np.float64)
            have = self._in[j0:j0 + step]
            n_have = min(step, have.shape[0])
            if n_have > 0:
                v[:n_have] = have[:n_have]
            self._voc_tail = None
            self._acc_tail = None
            self._advance(step)
            return (v, np.zeros((step, 2)))

        # ---- 裁出本块对应的输出区间 ----
        # voc_c[i] 对应 chunk[i]，chunk[0] 的全局序号是 lo
        j0 = start_glob - lo
        j1 = j0 + take
        if j1 > voc_c.shape[0]:
            j1 = voc_c.shape[0]
        v = np.asarray(voc_c[j0:j1], dtype=np.float64)
        a2 = np.asarray(acc_c[j0:j1], dtype=np.float64)
        if v.shape[0] < take:
            pad = np.zeros((take - v.shape[0], 2))
            v = np.concatenate([v, pad])
            a2 = np.concatenate([a2, pad])

        # ---- 与上一块的尾巴交叉淡化 ----
        if self._voc_tail is not None and self._fade > 0 and \
                self._voc_tail.shape[0] == self._fade:
            f = np.linspace(0.0, 1.0, self._fade).reshape(-1, 1)
            v[:self._fade] = self._voc_tail * (1.0 - f) + v[:self._fade] * f
            a2[:self._fade] = self._acc_tail * (1.0 - f) + a2[:self._fade] * f

        # ---- 产出：末块全给，非末块扣掉尾部 fade ----
        if final or self._fade <= 0:
            out_v, out_a = v, a2
            self._voc_tail = None
            self._acc_tail = None
        else:
            out_v = v[:take - self._fade]
            out_a = a2[:take - self._fade]
            self._voc_tail = v[take - self._fade:].copy()
            self._acc_tail = a2[take - self._fade:].copy()

        # ---- 推进：产出位置前进 step，缓冲区裁掉用不上的前半 ----
        step = take if final else self._hop
        self._advance(step)
        self.info["separation"] = sep
        return (out_v, out_a)

    def _advance(self, step):
        """产出位置前进 step，并把缓冲区裁到"保留前文 ctx"为止。"""
        self._out_pos += step
        cut = self._out_pos - self._ctx - self._in_base
        if cut > 0:
            self._in = np.ascontiguousarray(self._in[cut:])
            self._in_base += cut
