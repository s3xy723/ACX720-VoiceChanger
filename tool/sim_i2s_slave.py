# -*- coding: utf-8 -*-
"""
sim_i2s_slave.py   i2s_slave.v 的行为级时序仿真（纯 Python，不依赖 xsim）

为什么要写它：本工程所在机器的 Vivado / xsim 会挂死，跑不了仿真，
而 I2S 位对齐（MSB 落在 LRCK 跳变后的第几个 BCLK）是整条链路上唯一
"错了就完全没声音"的高风险点。这里用离散事件把 rtl/i2s_slave.v 的
逻辑逐行复刻一遍，跟一个理想的 ES8388 主机模型对接，验证收发位序。

时钟（与本工程配置一致）：
    MCLK  12.500 MHz（本仿真用不到）
    BCLK   1.5625 MHz    周期 640 ns = 32 个 50MHz 周期
    LRCK  24.414 kHz     半周期 32 个 BCLK = 2048 个 50MHz 周期
    每声道 32 个 BCLK 时隙，16 bit 有效数据，I2S 标准（MSB 先行）

codec 侧约定（I2S 标准）：
    LRCK 在 BCLK 下降沿跳变；LRCK=0 为左声道
    ADCDAT 在 BCLK 下降沿更新，FPGA 在下一个上升沿采样
    时隙 0 无效；时隙 1..16 = bit15..bit0；时隙 17..31 = 0

运行：python tool/sim_i2s_slave.py
"""

STEP = 20                 # 50 MHz 系统时钟周期 (ns)
BCLK_PERIOD = 640         # ns
BCLK_HALF = BCLK_PERIOD // 2
FRAME_SLOTS = 32          # 每声道 32 个 BCLK
HALF_FRAME = FRAME_SLOTS * BCLK_PERIOD   # 20480 ns = 1024 个系统周期
NFRAMES = 4


class CodecMaster:
    """理想的 ES8388 主机：产生 BCLK / LRCK / ADCDAT，并采样 DACDAT"""

    def __init__(self, left_words, right_words):
        self.left_words = left_words
        self.right_words = right_words
        self.sampled_bits = []      # (frame, channel, slot, bit)
        self.frame = 0
        self.half = 0               # 0=左, 1=右

    def word_bits(self, ch, frame):
        w = (self.left_words if ch == 0 else self.right_words)[frame % len(self.left_words)]
        bits = [(w >> (15 - i)) & 1 for i in range(16)]
        return [0] + bits + [0] * (FRAME_SLOTS - 17)   # 32 个时隙

    def bclk(self, t):
        return 1 if (t % BCLK_PERIOD) >= BCLK_HALF else 0

    def lrck(self, t):
        # t=0 起第一个半帧为左声道（低电平），半帧长度 32 个 BCLK
        return 0 if ((t // HALF_FRAME) % 2 == 0) else 1

    def adcdat(self, t):
        # 在当前 BCLK 下降沿（t % 640 == 0）更新为下一个时隙的位
        slot = (t // BCLK_PERIOD) % FRAME_SLOTS
        ch = self.lrck(t)
        frame = t // (2 * HALF_FRAME)
        return self.word_bits(ch, frame)[slot]

    def sample_dacdat(self, t, bit):
        # 在每个 BCLK 上升沿（t % 640 == 320）锁存
        slot = ((t - BCLK_HALF) // BCLK_PERIOD) % FRAME_SLOTS
        ch = self.lrck(t)
        frame = t // (2 * HALF_FRAME)
        self.sampled_bits.append((frame, ch, slot, bit))


def sim():
    # ------------------------------------------------------------------
    #  测试向量：方便肉眼验证位序
    # ------------------------------------------------------------------
    left_words = [0xA5A5, 0x1234, 0x7FFF, 0x8000]
    right_words = [0x5A5A, 0x4321, 0x0001, 0xFFFF]
    codec = CodecMaster(left_words, right_words)

    # ---------------- RTL 状态（逐行复刻 i2s_slave.v）----------------
    bclk_s = [0, 0, 0]
    lrck_s = [0, 0, 0]
    adcdat_s = [0, 0]
    slot = 0
    rx_sr = 0
    tx_sr = 0
    rx_left = 0
    rx_right = 0
    rx_strobe = 0
    aud_dacdat = 0

    tx_left, tx_right = 0x0F0F, 0x3C3C   # FPGA 要发送的字（常量即可）

    events = []      # (t, rx_left, rx_right) 在 strobe 时记录
    total = NFRAMES * 2 * HALF_FRAME

    for t in range(0, total, STEP):
        # ---- 组合逻辑（用上一拍的同步器值）----
        b = codec.bclk(t)
        l = codec.lrck(t)
        d = codec.adcdat(t)

        bclk_rise = (bclk_s[2], bclk_s[1]) == (0, 1)
        bclk_fall = (bclk_s[2], bclk_s[1]) == (1, 0)
        lrck_rise = (lrck_s[2], lrck_s[1]) == (0, 1)
        lrck_fall = (lrck_s[2], lrck_s[1]) == (1, 0)
        lrck_edge = lrck_rise or lrck_fall
        lrck_lvl = lrck_s[1]          # 与 RTL 一致：延迟 1 拍的值

        # ---- 时序逻辑 ----
        rx_strobe = 0
        if lrck_edge:
            slot = 0
            rx_sr = 0
            tx_sr = tx_right if lrck_lvl else tx_left
            aud_dacdat = 0
        elif bclk_rise:
            if 1 <= slot <= 16:
                rx_sr = ((rx_sr << 1) | adcdat_s[1]) & 0xFFFF
            if slot == 17:
                if lrck_lvl == 0:
                    rx_left = rx_sr
                else:
                    rx_right = rx_sr
                    rx_strobe = 1
            slot = (slot + 1) & 0x3F
        elif bclk_fall:
            if 1 <= slot <= 16:
                aud_dacdat = (tx_sr >> 15) & 1
                tx_sr = ((tx_sr << 1) & 0xFFFF)
            else:
                aud_dacdat = 0

        # ---- 同步器更新（非阻塞，放在最后）----
        # RTL: bclk_s <= {bclk_s[1:0], aud_bclk}   -> 索引 0 最新、索引 2 最旧
        bclk_s = [b, bclk_s[0], bclk_s[1]]
        lrck_s = [l, lrck_s[0], lrck_s[1]]
        adcdat_s = [d, adcdat_s[0]]

        # ---- codec 在 BCLK 上升沿采样 DACDAT ----
        if (t % BCLK_PERIOD) == BCLK_HALF:
            codec.sample_dacdat(t, aud_dacdat)

        if rx_strobe:
            events.append((t, rx_left, rx_right))

    return codec, events, tx_left, tx_right


def check():
    codec, events, tx_left, tx_right = sim()
    ok = True

    print("=" * 68)
    print(" RX 通路：FPGA 解出的样本 vs codec 实际发送的样本")
    print("=" * 68)
    for i, (t, l, r) in enumerate(events):
        exp_l = codec.left_words[i % len(codec.left_words)]
        exp_r = codec.right_words[i % len(codec.right_words)]
        good = (l == exp_l) and (r == exp_r)
        ok &= good
        print(" frame %d  t=%6d ns   L=0x%04X(期望0x%04X)  R=0x%04X(期望0x%04X)  %s"
              % (i, t, l, exp_l, r, exp_r, "PASS" if good else "FAIL"))

    print()
    print("=" * 68)
    print(" TX 通路：codec 在每个 BCLK 上升沿采到的 DACDAT 位序")
    print("=" * 68)
    # 取第 1 帧（frame 0）左声道，把 32 个时隙拼成字符串
    def slots_of(frame, ch):
        return [(s, b) for (f, c, s, b) in codec.sampled_bits if f == frame and c == ch]

    for (frame, ch, name, exp_word) in [(1, 0, "左", tx_left), (1, 1, "右", tx_right)]:
        slots = sorted(slots_of(frame, ch))
        seq = "".join(str(b) for (s, b) in slots)
        exp_bits = "".join(str((exp_word >> (15 - i)) & 1) for i in range(16))
        # 时隙 0 必须为 0；时隙 1..16 = MSB..LSB；时隙 17..31 = 0
        good = (seq[0] == "0") and (seq[1:17] == exp_bits) and (set(seq[17:]) == {"0"})
        ok &= good
        print(" frame %d %s声道  32 时隙 = %s" % (frame, name, seq))
        print("      期望            = 0%s%s%s   %s"
              % (exp_bits, "0" * 15, "", "PASS" if good else "FAIL"))

    print()
    print("=" * 68)
    print(" 汇总：%s" % ("PASS" if ok else "FAIL"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(check())
