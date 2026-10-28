# -*- coding: utf-8 -*-
"""
dsp_core.py  ——  与 RTL 逐比特对齐的音频效果链

这里每一个运算都刻意写成 rtl/pitch_shift.v、rtl/effect_aux.v 的镜像：
同样的延迟线深度（4096）、同样的 512 点 Hann 窗 ROM、同样的 Q15 定点、
同样的两级饱和门限。目的是让上位机调出来的参数可以直接搬到板子上，
听感不会有偏差。

整数域约定
----------
整个链路在 int16 域做定点运算（-32768 .. 32767），
只在最外层的"文件读取 / 声卡读写"两侧转成 float32。

各模块与 RTL 的对应关系
-----------------------
  PitchShifter  <->  rtl/pitch_shift.v      双抽头 Hann 交叉淡化变调
  RingMod       <->  rtl/effect_aux.v/ring_mod   环形调制（机器人声）
  Echo          <->  rtl/effect_aux.v/echo       前馈梳状回声
  VoiceChain    <->  rtl/voice_proc.v            四选一输出 + 音量
  StereoChain   <->  rtl/voice_changer_top.v     ★ 左右声道各一条独立效果链
  VocalSplitter <->  （板上没有）人声/伴奏分离，见 vocal_split.py

与 RTL 已知的（无害）差异
-------------------------
1. echo：RTL 的读地址多打了一拍，实际延迟是 D+1 个采样点；这里按 D 实现。
   一个采样点 @48 kHz 只有 21 us，听不出来，差异已写在注释里。
2. pitch：phase 接近 2^16 时第一抽头延迟会算到 0，此时窗权重恰好也为 0，
   这里把延迟钳到 >= 1，避免索引出界；乘出来的贡献本来就是 0。
3. MODE_SPLIT（左人声 / 右伴奏）是纯上位机模式：板子读不了音频文件，而且
   逐帧基频估计 + 谐波掩蔽远不是 7 系列放得下的规模。
"""

import numpy as np

from vocal_split import VocalSplitter

# ---------------------------------------------------------------- 常量

SIZE = 4096                      # 延迟线深度，和 pitch_shift.v 的 AW=12 一致
MASK = SIZE - 1
HANN_N = 512                     # Hann 窗 ROM 点数，覆盖 d ∈ [0, 2048]
SINE_N = 256                     # 正弦 ROM 点数

ACC_POS_MAX = 2147418112         #  32767 * 65536
ACC_NEG_MIN = -2147483648        # -32768 * 65536

MODE_BYPASS = 0
MODE_RING = 1
MODE_PITCH = 2
MODE_ECHO = 3
MODE_PITCH_ECHO = 4              # 仅上位机有：板子只能四选一
MODE_SPLIT = 5                   # 仅上位机有：人声/伴奏分离（左人声、右伴奏）
MODE_AI_LOCAL = 6                # 仅上位机有：本机上的本地 AI 模型处理（离线、非实时）

MODE_NAMES = ["直通", "机器人（环形调制）", "变调", "回声", "变调 + 回声",
              "分离（左=人声 / 右=伴奏）",
              "本地 AI 处理（计算机上的模型）"]

# 板上没有的模式 -> 界面上不再显示 sw 对照
BOARD_MODES = (MODE_BYPASS, MODE_RING, MODE_PITCH, MODE_ECHO)


# ---------------------------------------------------------------- 两层结构
#
# 上面那套 MODE_* 是"一个数字一个模式"的平铺列表，加到第七个已经塞不下：
# 变调和分离各有两种引擎，再乘上实时/文件两种输入，平铺要 8 个常量。
# 所以改成「模块 × 引擎」两层，跟界面的两级单选一一对应：
#
#     模块（做什么）          引擎（谁算）
#     ├─ 变调                ├─ FPGA 直算      时域延迟线，与 RTL 逐比特对齐
#     │                      └─ FPGA 内部模型   流式相位声码器 + 共振峰保持
#     └─ 分离                ├─ FPGA 内部模型   中置提取 + 谐波掩蔽（vocal_split）
#                            └─ 计算机本地模型   demucs 分块流式（local_stream）
#
# 旧常量全部保留，test_host.py 里基于 MODE_* 的断言继续有效。

MODULE_PITCH = "pitch"
MODULE_SPLIT = "split"

MODULE_NAMES = [
    (MODULE_PITCH, "变调"),
    (MODULE_SPLIT, "分离"),
]

ENGINE_FPGA_DIRECT = "fpga_direct"     # 变调：时域双抽头延迟线（= RTL）
ENGINE_AGE = "age"                     # 变调：任意年龄 → 任意年龄（变调 + 音质着色）

ENGINE_FPGA_MODEL = "fpga_model"       # 变调：板载/内置小模型（流式频域）
ENGINE_LOCAL_MODEL = "local_model"     # 分离：本机神经网络（demucs 流式）

# 每个模块下有哪些引擎，顺序即界面上的显示顺序。
MODULE_ENGINES = {
    MODULE_PITCH: [ENGINE_FPGA_DIRECT, ENGINE_AGE],
    MODULE_SPLIT: [ENGINE_FPGA_DIRECT, ENGINE_LOCAL_MODEL],
}

ENGINE_NAMES = {
    (MODULE_PITCH, ENGINE_FPGA_DIRECT): "FPGA 直算（时域延迟线，与 RTL 一致）",
    (MODULE_PITCH, ENGINE_AGE): "年龄变声（任意年龄 → 任意年龄，参数下发到板子）",
    (MODULE_PITCH, ENGINE_FPGA_MODEL): "FPGA 内部小模型（频域 + 共振峰保持）",
    (MODULE_SPLIT, ENGINE_FPGA_DIRECT): "中置提取（左=人声 / 右=伴奏）",
    (MODULE_SPLIT, ENGINE_LOCAL_MODEL): "计算机本地模型（demucs，左=人声 / 右=伴奏）",
}
# 标题栏徽章用的短名（放不下全名，只留能区分的那几个字）
MODULE_NAMES_SHORT = {MODULE_PITCH: "变调", MODULE_SPLIT: "分离"}
ENGINE_NAMES_SHORT = {
    (MODULE_PITCH, ENGINE_FPGA_DIRECT): "FPGA 直算",
    (MODULE_PITCH, ENGINE_AGE): "年龄变声",
    (MODULE_PITCH, ENGINE_FPGA_MODEL): "FPGA 小模型",
    (MODULE_SPLIT, ENGINE_FPGA_DIRECT): "中置提取",
    (MODULE_SPLIT, ENGINE_LOCAL_MODEL): "本地模型",
}

SRC_REALTIME = "realtime"
SRC_FILE = "file"

SRC_NAMES = [
    (SRC_REALTIME, "实时变声（麦克风）"),
    (SRC_FILE, "文件变声（整段处理）"),
]

SPLIT_DEFAULTS = dict(
    depth=1.0,       # 分离深度：1 = 人声全取出
    floor=0.06,      # 中置掩蔽下限（实测 0.06 比 0 多 ~0.3 dB 且更自然）
    strength=1.0,    # 中置度取幂
    broadband=0.20,  # 非谐波中置成分（齿音/气声）保留比例
    harmonic=True,   # 谐波梳状掩蔽
    f0_hz=0.0,       # 两个音高候选的音域分界（0=自动；与性别无关）
    swap=False,      # 人声去右 / 伴奏去左
    bass_protect_hz=120.0,  # 伴奏低频保护：从伴奏里扣人声时不扣这个频率以下
)

RING_FREQ_HZ = [60, 100, 150, 220, 350, 500, 800, 1200]
RING_NCO_INC = [20616, 34360, 51539, 75591, 120259, 171798, 274877, 412315]
ECHO_GAIN_VAL = [8192, 12288, 16384, 20480]      # Q15: 0.25 / 0.375 / 0.5 / 0.625
ECHO_GAIN_NAMES = ["0.25", "0.375", "0.50", "0.625"]

DPHASE_MIN = -32                 # 低于此值，体素：延迟线不够长，会跑调
DPHASE_MAX = 64


# ---------------------------------------------------------------- ROM 生成

def make_hann_rom():
    """512 点 Hann 窗，与 tool/gen_wave_rom.py 及 rtl/wave_rom.v 完全一致。

    分母必须是 HANN_N（512）而不是 HANN_N-1（511）：
    只有以 512 为周期，互补关系 w(i) + w(i+256) == 65535 才会严格成立，
    交叉淡化才不会出现周期性的包络起伏（听感上的"呜呜"抖动）。
    """
    v = []
    for i in range(HANN_N):
        w = 0.5 - 0.5 * np.cos(2.0 * np.pi * i / HANN_N)
        q = int(round(65535.0 * w))
        v.append(0 if q < 0 else (65535 if q > 65535 else q))
    return np.array(v, dtype=np.int64)


def make_sine_rom():
    """256 点正弦表，16 位有符号（-32768 .. 32767）。"""
    v = []
    for i in range(SINE_N):
        s = np.sin(2.0 * np.pi * i / SINE_N)
        q = int(round(32767.0 * s))
        v.append(-32768 if q < -32768 else (32767 if q > 32767 else q))
    return np.array(v, dtype=np.int64)


_HANN = make_hann_rom()
_SINE = make_sine_rom()


def _wrap16(v):
    """保留 int16 语义的低 16 位（对应 Verilog 里的 x[15:0] 位选）。"""
    return ((np.asarray(v, dtype=np.int64) + 0x8000) & 0xFFFF) - 0x8000


def _sat16(v):
    return np.clip(np.asarray(v, dtype=np.int64), -32768, 32767)


# ---------------------------------------------------------------- 变调

class PitchShifter(object):
    """双抽头 Hann 交叉淡化延迟线 —— rtl/pitch_shift.v 的逐比特移植。

    相位累加器 phase 是 16 位无符号小数，代表 p ∈ [0,1)。
    每个输出样本加一次 dphase（有符号），溢出自然回绕。
        变调比  r = 1 + dphase / 64
         dphase =  0 -> 1.000   原声
         dphase = 64 -> 2.000   升八度
         dphase =-32 -> 0.500   降八度
    """

    def __init__(self):
        # hist 按时间顺序存放最近 SIZE 个样本，hist[-1] 是最新一个。
        # 初始化为 0，与 BRAM 上电值一致。
        self.hist = np.zeros(SIZE, dtype=np.int64)
        self.phase = 0

    def reset(self):
        self.hist[:] = 0
        self.phase = 0

    def process_block(self, x, dphase):
        """x: int16 样本的一维数组  ->  int16 数组，长度相同。

        延迟 oriented 处理的向量化写法：把历史样本和新样本拼成一条
        按时间顺序排列的长数组，那么第 i 个输出要读的位置就是
        SIZE + i - delay。历史和二维数组都不需要再做取模。
        """
        x = np.asarray(x, dtype=np.int64)
        m = x.size
        i = np.arange(m, dtype=np.int64)

        # 注意这里的 Python 陷阱：>> 的优先级低于 +，
        # 所有移位都必须自己加括号，否则会静默变成 (a+b) >> k。
        ph = (self.phase + i * dphase) % 65536
        X = 65536 - ph                       # [1, 65536]
        Y = 131072 - ph                      # [65537, 131072]

        dly_a = np.maximum(X >> 6, 1)        # 0..1024，钳到 >=1
        dly_b = Y >> 6                       # 1024..2048
        frac_a = X & 63
        frac_b = Y & 63
        idx_a = (X >> 8) & 511               # 0..256
        idx_b = np.minimum(Y >> 8, 511)      # 256..512 -> 511

        ext = np.concatenate([self.hist, x])  # 长度 SIZE + m
        n0 = SIZE
        pa = n0 + i - dly_a
        pb = n0 + i - dly_b

        sa0 = ext[pa]
        sa1 = ext[pa - 1]
        sb0 = ext[pb]
        sb1 = ext[pb - 1]

        # 线性插值 step = (diff * frac) >>> 6
        tap_a = _wrap16(sa0 + (((sa1 - sa0) * frac_a) >> 6))
        tap_b = _wrap16(sb0 + (((sb1 - sb0) * frac_b) >> 6))

        wa = _HANN[idx_a]
        wb = _HANN[idx_b]

        acc = tap_a * wa + tap_b * wb        # 可能达到 ±2^31，用 int64 接
        y = np.where(acc > ACC_POS_MAX, 32767,
                     np.where(acc < ACC_NEG_MIN, -32768, acc >> 16))

        self.hist = ext[-SIZE:]
        self.phase = (self.phase + m * dphase) % 65536
        return y.astype(np.int16)


# ---------------------------------------------------------------- 环形调制

class RingMod(object):
    """环形调制 —— rtl/effect_aux.v 里 ring_mod 的移植。

    输出 = 0.5*干声 + 0.5*(干声 × 正弦)，保留一部分语言可懂度。
    相位累加器 24 位，ROM 地址取高 8 位。
    """

    def __init__(self):
        self.ph = 0

    def reset(self):
        self.ph = 0

    def process_block(self, x, freq_sel):
        x = np.asarray(x, dtype=np.int64)
        m = x.size
        inc = RING_NCO_INC[int(freq_sel) & 7]
        i = np.arange(m, dtype=np.int64)
        # RTL 里是先出结果再累加相位，所以第 i 个样本用的是累加前的相位
        ph = (self.ph + i * inc) % (1 << 24)
        s = _SINE[ph >> 16]

        wet = (x * s) >> 15                  # Q15 x Q15 -> Q15
        mixed = x + wet                      # 0.5*干 + 0.5*湿，最后一步除 2
        y = mixed >> 1
        self.ph = (self.ph + m * inc) % (1 << 24)
        return y.astype(np.int16)


# ---------------------------------------------------------------- 回声

class Echo(object):
    """前馈梳状回声 —— rtl/effect_aux.v 里 echo 的移植。

        dout = din + gain * x(n - D)

    4096 点 @48 kHz ≈ 85 ms；delay_len = taps * 256。
    RTL 的读地址还多打了一拍（实际 D+1），这里按 D 实现，差 21 us。
    """

    def __init__(self):
        self.hist = np.zeros(SIZE, dtype=np.int64)

    def reset(self):
        self.hist[:] = 0

    def process_block(self, x, taps, gain_sel):
        x = np.asarray(x, dtype=np.int64)
        m = x.size
        d = int(taps) * 256
        d = 1 if d < 1 else (SIZE - 1 if d > SIZE - 1 else d)
        g = ECHO_GAIN_VAL[int(gain_sel) & 3]

        ext = np.concatenate([self.hist, x])
        i = np.arange(m, dtype=np.int64)
        delayed = ext[SIZE + i - d]          # x(n - D)
        wet = (delayed * g) >> 15            # Q15 加权
        y = np.clip(x + wet, -32768, 32767)

        self.hist = ext[-SIZE:]
        return y.astype(np.int16)


# ---------------------------------------------------------------- 效果链

class VoiceChain(object):
    """单声道效果链 —— 对应 rtl/voice_proc.v。

    与板子一样，切到非变调模式时会自动复位变调延迟线和相位，
    避免相位停在半路导致下次切回来有咔哒声。
    """

    def __init__(self):
        self.ps = PitchShifter()
        self.rm = RingMod()
        self.ec = Echo()
        self.mode = MODE_PITCH
        self.dphase = 0
        self.ring_freq = 1
        self.echo_taps = 8
        self.echo_gain = 2
        self.gain = 1.0
        self._prev_mode = None

    def reset(self):
        self.ps.reset()
        self.rm.reset()
        self.ec.reset()
        self._prev_mode = None

    def configure(self, mode=MODE_PITCH, dphase=0, ring_freq=1,
                  echo_taps=8, echo_gain=2, gain=1.0):
        self.mode = int(mode)
        self.dphase = int(dphase)
        self.ring_freq = int(ring_freq) & 7
        self.echo_taps = int(echo_taps)
        self.echo_gain = int(echo_gain) & 3
        self.gain = float(gain)

    def process_block(self, x):
        """x: int16 ndarray -> int16 ndarray"""
        if self._prev_mode is not None and self._prev_mode != self.mode:
            self.reset()
        self._prev_mode = self.mode

        x = np.asarray(x, dtype=np.int16)
        mode = self.mode

        if mode == MODE_BYPASS:
            y = np.asarray(x, dtype=np.int64)
        elif mode == MODE_RING:
            y = np.asarray(self.rm.process_block(x, self.ring_freq), dtype=np.int64)
        elif mode == MODE_PITCH:
            y = np.asarray(self.ps.process_block(x, self.dphase), dtype=np.int64)
        elif mode == MODE_ECHO:
            y = np.asarray(self.ec.process_block(x, self.echo_taps, self.echo_gain),
                           dtype=np.int64)
        elif mode == MODE_PITCH_ECHO:
            t = self.ps.process_block(x, self.dphase)
            y = np.asarray(self.ec.process_block(t, self.echo_taps, self.echo_gain),
                           dtype=np.int64)
        else:
            y = np.asarray(x, dtype=np.int64)

        if abs(self.gain - 1.0) > 1e-9:
            y = np.clip(np.round(y * self.gain), -32768, 32767).astype(np.int64)
        return y.astype(np.int16)

    def process(self, x, block=4096):
        """分块处理任意长度信号。分块只是为了控制峰值内存，
        结果与一次性处理完全相同（状态跨块连续）。
        """
        x = np.asarray(x, dtype=np.int16)
        out = np.empty_like(x)
        for s in range(0, x.size, block):
            out[s:s + block] = self.process_block(x[s:s + block])
        return out


class StereoChain(object):
    """左右声道各挂一条独立的效果链 —— 板子上是每声道实例化一次，这里同理。

    ★ 双声道分别运算
        所有模式都是"左声道算一遍、右声道再算一遍"，两条链的延迟线、
        相位、回声历史完全独立，绝不共用状态，也绝不做 L/R 混音。
        输入几声道就按几声道算（>2 声道时只处理前两路，其余原样直通）。

    唯一的例外是 MODE_SPLIT（人声/伴奏分离）：它必须同时看两个声道才能
    做中置提取，因此走单独的 VocalSplitter，两条 VoiceChain 在这模式下不参与。
    """

    def __init__(self, sr=48000):
        self.ch = [VoiceChain(), VoiceChain()]
        self.split = VocalSplitter(sr)
        self.mode = MODE_PITCH
        self.gain = 1.0
        self.split_cfg = dict(SPLIT_DEFAULTS)

    def reset(self):
        for c in self.ch:
            c.reset()
        self.split.reset()

    def configure(self, mode=MODE_PITCH, dphase=0, ring_freq=1,
                  echo_taps=8, echo_gain=2, gain=1.0, split=None):
        self.mode = int(mode)
        self.gain = float(gain)
        for c in self.ch:
            c.configure(mode=mode, dphase=dphase, ring_freq=ring_freq,
                        echo_taps=echo_taps, echo_gain=echo_gain, gain=1.0)
        if split:
            self.split_cfg.update(split)
        self.split.configure(**self.split_cfg)

    def _apply_gain(self, y):
        if abs(self.gain - 1.0) < 1e-9:
            return y
        return np.clip(np.round(np.asarray(y, dtype=np.int64) * self.gain),
                       -32768, 32767).astype(np.int16)

    # ------------------------------------------------------------ 整段
    def process(self, x, block=4096):
        """x: shape (n,) 或 (n, c) 的 int16 数组。

        单声道输入会先复制成两路再分别运算（两条链状态独立），
        因此输出永远是立体声 —— 这是"所有输入输出都按双声道算"的落地点。
        """
        x = np.asarray(x, dtype=np.int16)
        if x.ndim == 1:
            x = np.repeat(x.reshape(-1, 1), 2, axis=1)
        elif x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)

        if self.mode == MODE_SPLIT:
            return self._apply_gain(self.split.process(x, block))

        n, c = x.shape
        out = np.empty_like(x)
        for j in range(c):
            src = np.ascontiguousarray(x[:, j])
            # 第 3 路起没有独立效果链：原样直通，不混进 L/R
            out[:, j] = (self.ch[j].process(src, block) if j < 2 else src)
        return self._apply_gain(out)

    # ------------------------------------------------------------ 流式
    def process_stream(self, x):
        """流式处理：返回本次能产出的样本。

        MODE_SPLIT 有 NFFT 点算法延迟，开头几次调用返回的样本数少于输入，
        调用方按"输出 FIFO"处理即可（RealtimeEngine 里有对应逻辑）。
        其余模式严格等长返回。
        """
        x = np.asarray(x, dtype=np.int16)
        if x.ndim == 1:
            x = np.repeat(x.reshape(-1, 1), 2, axis=1)
        elif x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)

        if self.mode == MODE_SPLIT:
            y = self.split.push(x.astype(np.float64) / 32768.0)
            if y.shape[0] == 0:
                return np.zeros((0, 2), dtype=np.int16)
            yi = np.clip(np.round(y * 32767.0), -32768, 32767).astype(np.int16)
            return self._apply_gain(yi)

        n, c = x.shape
        out = np.empty_like(x)
        for j in range(c):
            src = np.ascontiguousarray(x[:, j])
            out[:, j] = (self.ch[j].process_block(src) if j < 2 else src)
        return self._apply_gain(out)

    def process_stems(self, x, block=8192, progress=None):
        """整段跑一遍人声/伴奏分离，同时拿到两条独立音轨。

        只有 MODE_SPLIT 有意义（其余模式返回 (out, None)）。
        返回 ``(y, stems)``，stems 为 None 或
        ``{"vocal": (n,), "accomp": (n,)}``（两条都是单声道）。
        输出音量在这里统一施加，和 process() 保持一致。
        """
        x = np.asarray(x, dtype=np.int16)
        if x.ndim == 1:
            x = np.repeat(x.reshape(-1, 1), 2, axis=1)
        elif x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        if self.mode != MODE_SPLIT:
            return self.process(x, block), None
        y, st = self.split.process(x, block=block, stems=True, progress=progress)
        return self._apply_gain(y), st

    def configure_split(self, **kw):
        self.split_cfg.update(kw)
        self.split.configure(**self.split_cfg)


# ---------------------------------------------------------------- 参数换算

def semitone_to_dphase(semi):
    """半音数 -> dphase步进量。r = 2^(semi/12)，dphase = 64*(r-1)。"""
    r = 2.0 ** (float(semi) / 12.0)
    dp = int(round(64.0 * (r - 1.0)))
    return max(DPHASE_MIN, min(DPHASE_MAX, dp))


def dphase_to_ratio(dp):
    return 1.0 + float(dp) / 64.0


def ratio_to_semitone(r):
    import math
    return 12.0 * math.log(r, 2) if r > 0 else 0.0


def dphase_to_switch(dp):
    """反推板子上 sw[5:2] 的 0..15 档位（top 里是 (v-5)*6）。

    上位机可以给出任意 dphase，板子的 16 档只能取最接近的那一档，
    这个函数告诉你该拨到几。
    """
    v = int(round(float(dp) / 6.0)) + 5
    return max(0, min(15, v))


def switch_to_dphase(v):
    return (int(v) - 5) * 6


# ---------------------------------------------------------------- 统一分发器

def _to_i16(y):
    """float 域结果 -> int16 (m,2)。空数组按 (0,2) 返回。

    单独抽一个函数是因为这条换算在 EngineChain 里出现了四次，而其中三处
    曾经漏掉 `.astype(np.int16)`：float 域算法（hq_stream / local_stream /
    demucs 降级直通）的产出被当成 int16 传给 audio_io.to_float，
    `np.asarray(x, dtype=np.int16)` 一 cast，[-1,1] 的浮点全变成 0 / ±1。
    """
    y = np.asarray(y)
    if y.size == 0:
        return np.zeros((0, 2), dtype=np.int16)
    if y.ndim == 1:
        y = np.repeat(y.reshape(-1, 1), 2, axis=1)
    return np.clip(np.round(y * 32767.0), -32768, 32767).astype(np.int16)


class EngineChain(object):
    """「模块 × 引擎 × 输入」的统一入口，界面上只跟这一个类打交道。

    四种组合各自落到已有的实现上，不重写算法：

        变调 × FPGA 直算      -> VoiceChain（int16 域，与 rtl/pitch_shift.v 对齐）
        变调 × FPGA 内部模型  -> hq_stream.StreamPitchShifter（float 域）
        分离 × FPGA 内部模型  -> VocalSplitter（int16 域，左人声右伴奏）
        分离 × 计算机本地模型 -> local_stream.StreamSeparator（demucs 分块流式）
                                  可选再挂一级 hq_stream 处理人声，然后重混

    ★ 域的换算集中在这里做：VoiceChain / VocalSplitter 吃 int16，
      hq_stream / local_stream 吃 float64。进出各转一次，算法本身不改。

    ★ 降级是显式的：demucs 缺依赖时 local_stream 自己退化为直通并在
      info 里写明原因；这里再把原因透传给界面，不静默、不崩。
    """

    def __init__(self, sr=48000):
        self.sr = int(sr)
        self.module = MODULE_PITCH
        self.engine = ENGINE_FPGA_DIRECT
        self.source = SRC_REALTIME
        # 变调参数
        self.semi = 0.0
        self.formant = 1.0
        # 分离参数
        self.split_cfg = dict(SPLIT_DEFAULTS)
        self.vocal_gain = 1.0
        self.acc_gain = 1.0            # 左=人声 / 右=伴奏，默认两路都出声
        self.gain = 1.0                # 总输出音量（所有引擎共用）
        self.ai_quality = "std"
        self.ai_device = "auto"
        # 子模块（按需创建，切模式时重建）
        self._chain = None             # VoiceChain
        self._splitter = None          # VocalSplitter
        self._hq = None                # StreamPitchShifter
        self._sep = None               # StreamSeparator
        # 已经建好子模块的 (module, engine)。**不能**用 `self._chain is None`
        # 判断"要不要重建"：四种组合里只有第一种会创建 _chain，其余三种
        # _chain 永远是 None，那样每调一次 configure 就整个重建一遍，
        # 流式状态（OLA/相位/块缓冲）全被抹掉 —— 实时模式下就是每音频块
        # 重置一次算法，输出必然是错的。
        self._built = None
        self._need_flush = False
        self._warm_left = 0            # 还剩多少预热零没喂
        self._fifo = np.zeros((0, 2), dtype=np.int16)
        self.info = {"engine": [], "degraded": False}

    # ------------------------------------------------------------ 配置

    def configure(self, module=MODULE_PITCH, engine=ENGINE_FPGA_DIRECT,
                  source=SRC_REALTIME, semi=0.0, formant=1.0,
                  split=None, vocal_gain=None, acc_gain=None,
                  ai_quality=None, ai_device=None, dphase=None,
                  ring_freq=1, echo_taps=8, echo_gain=2, gain=1.0):
        self.module = module
        self.engine = engine
        self.source = source
        self.semi = float(semi)
        self.formant = float(formant)
        self.gain = float(gain)
        if vocal_gain is not None:
            self.vocal_gain = float(vocal_gain)
        if acc_gain is not None:
            self.acc_gain = float(acc_gain)
        if ai_quality:
            self.ai_quality = ai_quality
        if ai_device:
            self.ai_device = ai_device
        if split:
            self.split_cfg.update(split)
        # 组合变了才重建子模块（参数变了就地改，不丢状态）
        key = (self.module, self.engine)
        if key != self._built:
            self._build()
            self._built = key
        else:
            self._retune(dphase=dphase, ring_freq=ring_freq,
                         echo_taps=echo_taps, echo_gain=echo_gain, gain=gain)

    def _build(self):
        """按当前 模块×引擎 建子模块。"""
        self._chain = self._splitter = self._hq = self._sep = None
        self._need_flush = False
        self._fifo = np.zeros((0, 2), dtype=np.int16)
        # 只有"单次产出可能不等于输入"的组合需要预热：
        # 时域延迟线（VoiceChain）严格等长，demucs 自己管理缓存。
        self._warm_left = (self.WARMUP
                           if (self.module == MODULE_PITCH and
                               self.engine == ENGINE_FPGA_MODEL) or
                           (self.module == MODULE_SPLIT and
                            self.engine == ENGINE_FPGA_DIRECT)
                           else 0)
        self.info = {"engine": [], "degraded": False}
        if self.module == MODULE_PITCH:
            # 「年龄变声」和「FPGA 直算」在 DSP 上是同一条时域延迟线，
            # 区别只在于 dphase 由年龄换算得到、并且多一路音质着色
            # （气声/微颤/倾斜，参数已经通过 AA 55 包下发给板子了）。
            if self.engine in (ENGINE_FPGA_DIRECT, ENGINE_AGE):
                self._chain = StereoChain(self.sr)
                self._chain.configure(mode=MODE_PITCH, dphase=0,
                                      split=self.split_cfg)
                self.info["engine"].append(
                    "变调：年龄变声（时域延迟线 + 年龄参数）"
                    if self.engine == ENGINE_AGE else
                    "变调：FPGA 直算（时域延迟线）")
            else:
                import hq_stream
                self._hq = hq_stream.StreamPitchShifter(
                    self.sr, hq_ratio(self.semi), self.formant)
                self.info["engine"].append(
                    "变调：FPGA 内部小模型（流式相位声码器 + 共振峰保持）")
        else:
            if self.engine == ENGINE_FPGA_DIRECT:
                self._splitter = VocalSplitter(self.sr)
                # 唯一语义：左 = 人声，右 = 伴奏（2026-10-21 起'
                # 旧的男左女右路由已从 VocalSplitter 里整体删除）。
                sc = dict(self.split_cfg)
                sc.pop("route", None)
                self._splitter.configure(**sc)
                self.info["engine"].append("分离：中置提取（左=人声 / 右=伴奏）")
            else:
                import local_stream
                self._sep = local_stream.StreamSeparator(
                    self.sr, device=self.ai_device, quality=self.ai_quality)
                self.info["engine"].extend(self._sep.info["engine"])
                self.info["degraded"] = self._sep.info["degraded"]
                self.info["sep_latency_s"] = self._sep.latency_s()
                if abs(self.semi) > 1e-9:
                    import hq_stream
                    self._hq = hq_stream.StreamPitchShifter(
                        self.sr, hq_ratio(self.semi), self.formant)
                    self.info["engine"].append(
                        "人声后处理：高质量变调 %+.1f 半音" % self.semi)

    def _retune(self, dphase=None, ring_freq=1, echo_taps=8, echo_gain=2,
                gain=1.0):
        """不换组合时的参数热更新。"""
        if self._chain is not None:
            self._chain.configure(mode=MODE_PITCH,
                                  dphase=dphase if dphase is not None else 0,
                                  ring_freq=ring_freq, echo_taps=echo_taps,
                                  echo_gain=echo_gain, gain=gain,
                                  split=self.split_cfg)
        if self._hq is not None:
            self._hq.set_ratio(hq_ratio(self.semi))
            self._hq.set_formant(self.formant)
        if self._splitter is not None:
            # 分离参数是"改了立刻生效"的：深度/非谐波/谐波梳都不需要重建
            # 流式状态（延迟线、帧网格还在跑），只更新掩蔽参数。
            sc = dict(self.split_cfg)
            sc.pop("route", None)
            self._splitter.configure(**sc)

    def reset(self):
        if self._chain is not None:
            self._chain.reset()
        if self._splitter is not None:
            self._splitter.reset()
        if self._hq is not None:
            self._hq.reset()
        if self._sep is not None:
            # ★ local_stream 是在 _build() 里局部 import 的，这里必须
            #   重新 import 一次才能拿到模块对象；直接写 local_stream.xxx
            #   会抛 NameError（_build() 没跑过时更是指向不存在）。
            import local_stream
            self._sep = local_stream.StreamSeparator(
                self.sr, device=self.ai_device, quality=self.ai_quality)
        self._need_flush = False
        # 状态清零之后，流式算法的前几十毫秒产出又会不足，所以预热计数
        # 要一起恢复 —— 否则"停止再启动"会先听到一段欠载爆音。
        self._fifo = np.zeros((0, 2), dtype=np.int16)
        self._warm_left = (self.WARMUP
                           if (self.module == MODULE_PITCH and
                               self.engine == ENGINE_FPGA_MODEL) or
                           (self.module == MODULE_SPLIT and
                            self.engine == ENGINE_FPGA_DIRECT)
                           else 0)

    # ------------------------------------------------------------ 整段

    def process(self, x, block=4096, progress=None):
        """整段处理。x: (n,) 或 (n,c) int16；返回同形状 int16。"""
        x = np.asarray(x, dtype=np.int16)
        mono = (x.ndim == 1)
        a = x.reshape(-1, 1) if mono else x
        if a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        if self.module == MODULE_PITCH:
            if self._chain is not None:
                y = self._chain.process(a, block)
            else:
                y = self._run_hq_offline(a)
        else:
            if self._splitter is not None:
                y = self._splitter.process(a, block=block, progress=progress)
                y = self._stem_gain(y)
            else:
                y = self._run_sep_offline(a, progress=progress)
        y = np.clip(np.asarray(y), -32768, 32767).astype(np.int16)
        return y[:, 0] if mono else y

    def _stem_gain(self, y):
        """分离 → 人声/伴奏分别给增益（左=人声、右=伴奏）。

        ★ 不能把两路都乘同一个 gain：用户要的是"人声大一点、伴奏小一点"
          这种独立配平，而全局 gain 只该调总输出。两者是串联关系。
        """
        a = np.asarray(y, dtype=np.float64)
        if a.ndim != 2 or a.shape[1] < 2:
            return y
        vg = float(getattr(self, "vocal_gain", 1.0))
        ag = float(getattr(self, "acc_gain", 1.0))
        if abs(vg - 1.0) < 1e-9 and abs(ag - 1.0) < 1e-9 and \
                abs(float(getattr(self, "gain", 1.0)) - 1.0) < 1e-9:
            return y
        out = a.copy()
        out[:, 0] *= vg * float(getattr(self, "gain", 1.0))
        out[:, 1] *= ag * float(getattr(self, "gain", 1.0))
        return np.clip(out, -32768.0, 32767.0)

    def _run_hq_offline(self, a):
        """文件模式下变调走离线 HQ（质量最好，不用流式状态机）。"""
        import hq_voice
        f = a.astype(np.float64) / 32768.0
        cols = [hq_voice.process(np.ascontiguousarray(f[:, c]), self.sr,
                                 hq_ratio(self.semi), formant=self.formant)
                for c in range(2)]
        y = np.stack(cols, axis=1)
        return np.clip(np.round(y * 32767.0 * self.gain), -32768, 32767)

    def _run_sep_offline(self, a, progress=None):
        """文件模式下本地模型分离走离线整段 local_ai.run（质量最好）。"""
        import local_ai
        f = a.astype(np.float64) / 32768.0
        y, info = local_ai.run(
            f, self.sr,
            dict(use_demucs=True, voice=("hq" if self._hq is not None
                                         else "none"),
                 semi=self.semi, formant=self.formant,
                 vocal_gain=self.vocal_gain, acc_gain=self.acc_gain,
                 device=self.ai_device, quality=self.ai_quality,
                 # ★ 分离模块的语义：左=人声、右=伴奏
                 route_stems=(self.module == MODULE_SPLIT)),
            progress=progress)
        self.info["engine"] = list(info["engine"])
        self.info["degraded"] = bool(info.get("warn"))
        if info.get("warn"):
            self.info["why"] = info["warn"]
            if "跳过" in str(info["warn"]):
                # 没做分离就原样返回，别把"直通"变成"只有左声道"
                return np.asarray(a)
        return np.clip(np.round(np.asarray(y) * 32767.0 * float(self.gain)),
                       -32768, 32767)

    # ------------------------------------------------------------ 流式

    # 预热样本数。流式算法的前几十毫秒产出不足（OLA/相位声码器要攒够
    # 一帧才吐第一个样本），直接拿去放会欠载爆音。先用这么多零把内部
    # 状态建起来，之后严格 1:1。实测 pitch×fpga_model 缺口 1792~1984、
    # split×fpga_direct 缺口 768~960，取 4096 覆盖两者。
    WARMUP = 4096

    def process_stream(self, x):
        """流式处理：喂一块 int16，拿回**等长**的一块 int16。

        内部做两件事保证播放平滑：
          1. 首次调用前先喂 WARMUP 个零做预热（输出一起进 FIFO，
             表现为约 85 ms 的启动延迟，不丢真实音频）；
          2. 输出 FIFO 配平 —— 算法本次产得多就存着，产得少就补零，
             永远返回与输入等长的一段，播放端不需要知道算法细节。
        """
        x = np.asarray(x, dtype=np.int16)
        mono = (x.ndim == 1)
        a = x.reshape(-1, 1) if mono else x
        if a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        n_in = a.shape[0]

        if self._warm_left > 0:
            w = min(self._warm_left, 8192)
            self._feed(np.zeros((w, 2), dtype=np.int16))
            self._warm_left = max(0, self._warm_left - w)

        y = self._feed(a)
        # ---- FIFO 配平 ----
        self._fifo = (np.concatenate([self._fifo, y]) if self._fifo.size
                      else np.asarray(y))
        if self._fifo.shape[0] >= n_in:
            out = self._fifo[:n_in]
            self._fifo = np.ascontiguousarray(self._fifo[n_in:])
        else:
            pad = np.zeros((n_in - self._fifo.shape[0], 2), dtype=np.int16)
            out = np.concatenate([self._fifo, pad]) if self._fifo.size else pad
            self._fifo = np.zeros((0, 2), dtype=np.int16)
        return out[:, 0] if mono else out

    def _feed(self, a):
        """真正跑一遍算法，返回 int16 (m,2)。

        ★ 返回值**必须**是 int16。曾经 _hq_stream / _sep_stream 直接返回
          np.clip(np.round(y*32767), ...) 的 float64，甚至把 demucs 降级
          路径的 float32（值域 [-1,1]）原样返回；上游 audio_io.to_float 做
          `np.asarray(x, dtype=np.int16)` 一cast，[-1,1] 的浮点全变成
          0 / ±1 —— 表现为"选了本地模型，实时输出全是静音"。
        """
        if self.module == MODULE_PITCH:
            if self._chain is not None:
                return self._chain.process_stream(a)
            return self._hq_stream(a)
        if self._splitter is not None:
            y = self._splitter.push(a.astype(np.float64) / 32768.0)
            if y.shape[0] == 0:
                return np.zeros((0, 2), dtype=np.int16)
            yf = self._stem_gain(y * 32768.0) / 32768.0
            return _to_i16(yf)
        return self._sep_stream(a)

    def _hq_stream(self, a):
        f = a.astype(np.float64) / 32768.0
        y = self._hq.push(f)
        if y.size and abs(float(self.gain) - 1.0) > 1e-9:
            y = np.clip(y * float(self.gain), -1.0, 1.0)
        return _to_i16(y) if y.size else np.zeros((0, 2), dtype=np.int16)

    def _sep_stream(self, a):
        # demucs 不可用 / 推理失败时 local_stream 会退化为"voc=输入、acc=静音"。
        # 在"左人声 / 右伴奏"语义下那会让右声道全静音，听感像坏了；
        # 这里显式原样返回双声道输入，才是真正的"直通降级"。
        if not getattr(self._sep, "available", True) or \
                bool(self._sep.info.get("degraded")):
            self.info["engine"] = list(self._sep.info["engine"])
            self.info["degraded"] = True
            return np.ascontiguousarray(a)
        f = a.astype(np.float64) / 32768.0
        voc, acc = self._sep.push(f)
        self.info["engine"] = list(self._sep.info["engine"])
        self.info["degraded"] = self._sep.info["degraded"]
        self.info["sep_latency_s"] = self._sep.latency_s()
        if voc.shape[0] == 0:
            return np.zeros((0, 2), dtype=np.int16)
        # 人声后处理（可选）+ 重混
        if self._hq is not None:
            v = self._hq.push(voc)
        else:
            v = voc
        n = min(v.shape[0], acc.shape[0])
        if n == 0:
            return np.zeros((0, 2), dtype=np.int16)
        v = v[:n]
        acc = acc[:n]
        if self.module == MODULE_SPLIT:
            # 左 = 人声、右 = 伴奏。两路都是单声道内容，各自占一个物理
            # 声道；不再把两轨混回左右两边（那是旧版的"重混"语义）。
            # ★ swap：中置提取那条路在 VocalSplitter._emit 里处理了，
            #   本地模型这条原来漏了 —— 同一个勾选框在两种引擎下效果不同。
            v_mono = 0.5 * (v[:, 0] + v[:, 1])
            a_mono = 0.5 * (acc[:, 0] + acc[:, 1])
            y = np.empty((n, 2), dtype=np.float64)
            if bool(self.split_cfg.get("swap", False)):
                y[:, 0] = a_mono * self.acc_gain
                y[:, 1] = v_mono * self.vocal_gain
            else:
                y[:, 0] = v_mono * self.vocal_gain
                y[:, 1] = a_mono * self.acc_gain
            return _to_i16(y * float(self.gain))
        y = v * self.vocal_gain + acc * self.acc_gain
        return _to_i16(y * float(self.gain))

    def flush(self):
        """流式收尾，返回剩余输出（实时模式下从不调用）。"""
        # ★ 顺序有讲究：必须先收 _sep 再收 _hq。原来 _hq 排在最前面，
        # 「分离 × 本地模型 + 人声变调」时 _sep 里 demucs 的尾部块会被
        # 整体丢掉，表现为处理结果的最后几百毫秒突然没了。
        if self._sep is not None:
            voc, acc = self._sep.flush()
            if voc.shape[0] == 0:
                # 人声已空，但 _hq 里可能还压着没吐完的尾块
                if self._hq is not None:
                    tail = self._hq.flush()
                    return (_to_i16(tail) if tail.shape[0]
                            else np.zeros((0, 2), dtype=np.int16))
                return np.zeros((0, 2), dtype=np.int16)
            if self._hq is not None:
                a = self._hq.push(voc)
                b = self._hq.flush()
                if a.shape[0] and b.shape[0]:
                    v = np.concatenate([a, b], axis=0)
                else:
                    v = a if a.shape[0] else b
            else:
                v = voc
            n = min(v.shape[0], acc.shape[0]) if v.shape[0] else 0
            if n == 0:
                return np.zeros((0, 2), dtype=np.int16)
            if self.module == MODULE_SPLIT:
                # 与 _sep_stream 保持一致：这里也要认 swap
                v_mono = 0.5 * (v[:n, 0] + v[:n, 1])
                a_mono = 0.5 * (acc[:n, 0] + acc[:n, 1])
                y = np.empty((n, 2), dtype=np.float64)
                if bool(self.split_cfg.get("swap", False)):
                    y[:, 0] = a_mono * self.acc_gain
                    y[:, 1] = v_mono * self.vocal_gain
                else:
                    y[:, 0] = v_mono * self.vocal_gain
                    y[:, 1] = a_mono * self.acc_gain
                return _to_i16(y * float(self.gain))
            return _to_i16((v[:n] * self.vocal_gain + acc[:n] * self.acc_gain)
                           * float(self.gain))
        return np.zeros((0, 2), dtype=np.int16)

    def latency_ms(self):
        """当前组合的算法延迟（毫秒），界面如实显示。"""
        if self.module == MODULE_PITCH:
            if self._hq is not None:
                # 分析窗 n + hop + 重采样核半宽，量级 30 ms
                return 30.0
            return 0.0                      # 时域延迟线是采样级延迟
        if self._sep is not None:
            return self._sep.latency_s() * 1000.0
        if self._splitter is not None:
            return 1024.0 / self.sr * 1000.0    # NFFT 点
        return 0.0


def hq_ratio(semi):
    """半音数 -> 频率比。hq_voice 里有同名函数，这里包一层避免循环 import。"""
    return 2.0 ** (float(semi) / 12.0)
