# -*- coding: utf-8 -*-
"""
sim_i2s_mirror.py   rtl/i2s_mirror_tx.v 的行为级仿真（纯 Python，不依赖 xsim）

验证"FPGA -> 单片机"这条新链路的第一段。被测三件事：

  1. 镜像总线 mcu_dout 的位流与 aud_dacdat **逐位相同**
     （同一个时隙计数器、同一个移位寄存器、同一个装载时刻，
       单片机听到的和 codec 听到的是同一串比特）；
  2. 按 STM32F103 I2S 从机的规则解码镜像总线
     （mcu_bclk 上升沿锁存、LRCK 边沿后第 1 个时隙无效、
       第 2~17 个时隙取 16 位 MSB 先行），
     解出来的样本必须与 FPGA 装载进移位寄存器的样本逐位相同；
  3. 建立/保持裕量：mcu_dout 的每次翻转都离 mcu_bclk 上升沿足够远
     （同步器给数据和时钟的延迟不同，要确认没有把数据推到边沿附近）。

时钟（与本工程配置一致，见 rtl/i2s_slave.v 文件头）：
    BCLK   1.5625 MHz    周期 640 ns  = 32 个 50 MHz 周期
    LRCK  24.414  kHz    半周期 32 个 BCLK = 20480 ns
    每声道 32 个 BCLK 时隙，16 bit 有效数据，Philips 标准（MSB 先行）

被测模型
    CodecMaster     理想 ES8388 主机（与 sim_i2s_slave.py 同一个）
    SlaveRTL        rtl/i2s_slave.v 的逐行复刻（给出 aud_dacdat 与样本源）
    MirrorRTL       rtl/i2s_mirror_tx.v 的逐行复刻
    Stm32Decoder    按 STM32F103 I2S 从机规则解码 —— 这就是单片机看到的波形

运行：python tool/sim_i2s_mirror.py
"""

STEP = 20                 # 50 MHz 系统时钟周期 (ns)
BCLK_PERIOD = 640         # ns
BCLK_HALF = BCLK_PERIOD // 2
FRAME_SLOTS = 32          # 每声道 32 个 BCLK
HALF_FRAME = FRAME_SLOTS * BCLK_PERIOD   # 20480 ns
NFRAMES = 10

# 每帧不同的测试字：只要有一帧错位 / 串帧 / 左右装反，立刻就能看出来
LEFT_WORDS = [0xA5A5, 0x1234, 0x7FFF, 0x8000, 0x0F0F, 0xDEAD, 0x0001, 0xBEEF]
RIGHT_WORDS = [0x5A5A, 0x4321, 0x0001, 0xFFFF, 0x3C3C, 0xCAFE, 0x7FFF, 0x0101]


# ---------------------------------------------------------------- 被测模型

class CodecMaster:
    """理想的 ES8388 主机：产生 BCLK / LRCK / ADCDAT，并在 BCLK 上升沿采样 DACDAT"""

    def __init__(self, left_words, right_words):
        self.left_words = left_words
        self.right_words = right_words

    def word_bits(self, ch, frame):
        w = (self.left_words if ch == 0 else self.right_words)[frame % len(self.left_words)]
        bits = [(w >> (15 - i)) & 1 for i in range(16)]
        return [0] + bits + [0] * (FRAME_SLOTS - 17)   # 时隙 0 无效，17..31 补 0

    def bclk(self, t):
        return 1 if (t % BCLK_PERIOD) >= BCLK_HALF else 0

    def lrck(self, t):
        return 0 if ((t // HALF_FRAME) % 2 == 0) else 1

    def adcdat(self, t):
        slot = (t // BCLK_PERIOD) % FRAME_SLOTS
        return self.word_bits(self.lrck(t), t // (2 * HALF_FRAME))[slot]


class SlaveRTL:
    """rtl/i2s_slave.v 的逐行复刻（只保留与本次验证有关的部分）。

    效果链用 bypass 模型代替：rx_strobe 后 1 拍把收到的样本锁进
    tx_l_reg / tx_r_reg，模拟"处理有延迟但样本内容不变"。
    """

    def __init__(self):
        self.bclk_s = [0, 0, 0]
        self.lrck_s = [0, 0, 0]
        self.adcdat_s = [0, 0]
        self.slot = 0
        self.rx_sr = 0
        self.tx_sr = 0
        self.rx_left = 0
        self.rx_right = 0
        self.rx_strobe = 0
        self.aud_dacdat = 0
        self.tx_l_reg = 0
        self.tx_r_reg = 0
        self._proc_pending = None
        self.loads = []          # (t, ch, word) 发送移位寄存器的装载记录

    @property
    def tx_left(self):
        return self.tx_l_reg

    @property
    def tx_right(self):
        return self.tx_r_reg

    def step(self, t, b, l, d):
        bclk_rise = (self.bclk_s[2], self.bclk_s[1]) == (0, 1)
        bclk_fall = (self.bclk_s[2], self.bclk_s[1]) == (1, 0)
        lrck_rise = (self.lrck_s[2], self.lrck_s[1]) == (0, 1)
        lrck_fall = (self.lrck_s[2], self.lrck_s[1]) == (1, 0)
        lrck_edge = lrck_rise or lrck_fall
        lrck_lvl = self.lrck_s[1]

        self.rx_strobe = 0
        if lrck_edge:
            self.slot = 0
            self.rx_sr = 0
            self.tx_sr = self.tx_right if lrck_lvl else self.tx_left
            self.aud_dacdat = 0
            self.loads.append((t, lrck_lvl, self.tx_sr))
        elif bclk_rise:
            if 1 <= self.slot <= 16:
                self.rx_sr = ((self.rx_sr << 1) | self.adcdat_s[1]) & 0xFFFF
            if self.slot == 17:
                if lrck_lvl == 0:
                    self.rx_left = self.rx_sr
                else:
                    self.rx_right = self.rx_sr
                    self.rx_strobe = 1
            self.slot = (self.slot + 1) & 0x3F
        elif bclk_fall:
            if 1 <= self.slot <= 16:
                self.aud_dacdat = (self.tx_sr >> 15) & 1
                self.tx_sr = (self.tx_sr << 1) & 0xFFFF
            else:
                self.aud_dacdat = 0

        # ---- 效果链 bypass：strobe 后 1 拍锁进发送寄存器 ----
        if self._proc_pending is not None:
            self.tx_l_reg, self.tx_r_reg = self._proc_pending
            self._proc_pending = None
        if self.rx_strobe:
            self._proc_pending = (self.rx_left, self.rx_right)

        # ---- 同步器更新（非阻塞，放在最后）----
        self.bclk_s = [b, self.bclk_s[0], self.bclk_s[1]]
        self.lrck_s = [l, self.lrck_s[0], self.lrck_s[1]]
        self.adcdat_s = [d, self.adcdat_s[0]]


class MirrorRTL:
    """rtl/i2s_mirror_tx.v 的逐行复刻"""

    def __init__(self):
        self.bclk_s = [0, 0, 0]
        self.lrck_s = [0, 0, 0]
        self.slot = 0
        self.tx_sr = 0
        self.mcu_bclk = 0
        self.mcu_lrck = 0
        self.mcu_dout = 0
        self.loads = []          # (t, ch, word) 每次 lrck_edge 装载进 tx_sr 的字

    def step(self, t, b, l, tx_left, tx_right):
        bclk_rise = (self.bclk_s[2], self.bclk_s[1]) == (0, 1)
        bclk_fall = (self.bclk_s[2], self.bclk_s[1]) == (1, 0)
        lrck_rise = (self.lrck_s[2], self.lrck_s[1]) == (0, 1)
        lrck_fall = (self.lrck_s[2], self.lrck_s[1]) == (1, 0)
        lrck_edge = lrck_rise or lrck_fall
        lrck_lvl = self.lrck_s[1]

        if lrck_edge:
            self.slot = 0
            self.tx_sr = tx_right if lrck_lvl else tx_left
            self.mcu_dout = 0                       # 时隙 0 = 无效位
            self.loads.append((t, lrck_lvl, self.tx_sr))
        elif bclk_rise:
            self.slot = (self.slot + 1) & 0x3F      # 上升沿只推进计数
        elif bclk_fall:
            if 1 <= self.slot <= 16:                # 下降沿更新数据
                self.mcu_dout = (self.tx_sr >> 15) & 1
                self.tx_sr = (self.tx_sr << 1) & 0xFFFF
            else:
                self.mcu_dout = 0                   # 时隙 17..31 补 0

        # ---- 时钟线：同步器的寄存拷贝（非阻塞，取本拍旧值）----
        self.mcu_bclk = self.bclk_s[2]
        self.mcu_lrck = self.lrck_s[2]

        self.bclk_s = [b, self.bclk_s[0], self.bclk_s[1]]
        self.lrck_s = [l, self.lrck_s[0], self.lrck_s[1]]


class Stm32Decoder:
    """STM32F103 I2S 从机视角：mcu_bclk 上升沿采样 mcu_dout，
    mcu_lrck 边沿后第 1 个时隙无效，第 2~17 个时隙取 16 位 MSB 先行。"""

    def __init__(self):
        self.slot = 0
        self.sr = 0
        self.ch = 0
        self.prev_bclk = 0
        self.prev_lrck = 0
        self.started = False     # 真实 I2S 从机要等第一个 WS 边沿才开始收帧
        self.words = []          # (t, ch, word)
        self.slots = []          # 当前半帧已经采到的位（用于核对 32 时隙模式）
        self.half_frames = []    # 每半帧的 32 个位

    def step(self, t, b, l, d):
        if self.prev_lrck != l:                 # LRCK 边沿：新时隙开始
            if self.started and self.slots:
                self.half_frames.append((self.ch, self.slots))
            self.started = True
            self.slot = 0
            self.sr = 0
            self.ch = l                        # LRCK=0 -> 左声道
            self.slots = []
        if self.prev_bclk == 0 and b == 1:      # BCLK 上升沿：锁存
            if self.started:
                self.slots.append(d)
                if 1 <= self.slot <= 16:
                    self.sr = ((self.sr << 1) | d) & 0xFFFF
                    if self.slot == 16:
                        self.words.append((t, self.ch, self.sr))
            self.slot += 1
        self.prev_bclk = b
        self.prev_lrck = l


# ---------------------------------------------------------------- 仿真

def sim():
    codec = CodecMaster(LEFT_WORDS, RIGHT_WORDS)
    slave = SlaveRTL()
    mirror = MirrorRTL()
    mcu = Stm32Decoder()

    codec_bits = []      # codec 在 aud_bclk 上升沿采到的 aud_dacdat
    mirror_bits = []     # STM32 在 mcu_bclk 上升沿采到的 mcu_dout
    dout_change_t = []   # mcu_dout 每次翻转的时刻
    bclk_rise_t = []     # mcu_bclk 上升沿时刻
    prev_dout = 0
    prev_mcu_bclk = 0

    total = NFRAMES * 2 * HALF_FRAME
    for t in range(0, total, STEP):
        b = codec.bclk(t)
        l = codec.lrck(t)
        d = codec.adcdat(t)

        # codec 在自己的 BCLK 上升沿采样 aud_dacdat（注意在 step 之前取，
        # 这样采到的是"边沿时刻线上原有的值"，与真实锁存一致）
        if (t % BCLK_PERIOD) == BCLK_HALF:
            codec_bits.append(slave.aud_dacdat)

        slave.step(t, b, l, d)
        mirror.step(t, b, l, slave.tx_left, slave.tx_right)

        if mirror.mcu_dout != prev_dout:
            dout_change_t.append(t)
            prev_dout = mirror.mcu_dout

        # STM32 在镜像 BCLK 上升沿锁存。mcu.step 内部按同一规则收位，
        # 这里单独记录时刻与采样值，供"位流比对 / 裕量"两项断言使用。
        # prev_mcu_bclk 必须跨迭代保留（本拍与上一拍比较才是边沿）。
        mcu.step(t, mirror.mcu_bclk, mirror.mcu_lrck, mirror.mcu_dout)
        if prev_mcu_bclk == 0 and mirror.mcu_bclk == 1:
            bclk_rise_t.append(t)
            mirror_bits.append(mirror.mcu_dout)
        prev_mcu_bclk = mirror.mcu_bclk

    if mcu.slots:
        mcu.half_frames.append((mcu.ch, mcu.slots))

    return dict(codec=codec, slave=slave, mirror=mirror, mcu=mcu,
                codec_bits=codec_bits, mirror_bits=mirror_bits,
                dout_change_t=dout_change_t, bclk_rise_t=bclk_rise_t,
                total=total)


# ---------------------------------------------------------------- 断言

def check():
    r = sim()
    slave, mirror, mcu = r["slave"], r["mirror"], r["mcu"]
    codec_bits, mirror_bits = r["codec_bits"], r["mirror_bits"]
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print("  [%s] %s%s" % ("PASS" if good else "FAIL", name,
                               ("\n         " + detail) if detail else ""))

    print("=" * 74)
    print(" rtl/i2s_mirror_tx.v 行为级仿真 —— FPGA -> STM32 的 I2S 镜像链路")
    print("=" * 74)
    print("  BCLK 1.5625 MHz / LRCK 24.414 kHz / 32 时隙 / 16bit / Philips / %d 帧" % NFRAMES)

    # ------------------------------------------------------------
    #  [1] 位流一致性：单片机采到的 == codec 采到的
    # ------------------------------------------------------------
    print("\n[1] 镜像位流 vs codec 位流（核心：镜像，不是近似）")
    n = min(len(codec_bits), len(mirror_bits))
    report("两条位流长度一致", len(codec_bits) == len(mirror_bits),
           "codec %d 位 / 镜像 %d 位" % (len(codec_bits), len(mirror_bits)))
    diff = [i for i in range(n) if codec_bits[i] != mirror_bits[i]]
    report("逐位比对：%d 位全部相同" % n, not diff,
           ("首个不一致在第 %d 位" % diff[0]) if diff else "")

    # ------------------------------------------------------------
    #  [2] 时隙模式：LRCK 边沿后 32 个 BCLK 的位分布
    # ------------------------------------------------------------
    print("\n[2] Philips 时隙结构（时隙 0 无效，1..16 = bit15..bit0，17..31 = 0）")
    # 解码器从第一个 LRCK 边沿才开始收帧，所以 hf[i] 与 mirror.loads[i]
    # 一一对应；末尾那个半帧可能没走完 32 个时隙，不参与位模式核对。
    hf = mcu.half_frames
    report("半帧数量 >= %d" % (2 * NFRAMES - 3), len(hf) >= 2 * NFRAMES - 3,
           "实际 %d 个半帧" % len(hf))
    full = hf[:-1]
    bad_len = sum(1 for (_, s) in full if len(s) != FRAME_SLOTS)
    report("完整半帧恰好 32 个时隙", bad_len == 0,
           "%d 个完整半帧，非 32 时隙的：%d 个" % (len(full), bad_len))

    # 用稳态段（跳过前 2 个半帧的预热）逐半帧核对位模式
    def pat_ok(word, slots):
        if len(slots) != FRAME_SLOTS:
            return False
        if slots[0] != 0:
            return False
        if any(x != 0 for x in slots[17:]):
            return False
        got = 0
        for i in range(16):
            got = (got << 1) | slots[1 + i]
        return got == word

    checked = 0
    for i, ((_, ch_l, w), (ch, slots)) in enumerate(zip(mirror.loads, full)):
        checked += 1
        if ch_l != ch:
            report("半帧 %d 的声道极性" % i, False,
                   "装载 ch=%d，解码 ch=%d（LRCK=0 应为左）" % (ch_l, ch))
            break
        if not pat_ok(w, slots):
            report("半帧 %d 的 32 时隙位模式" % i, False,
                   "装载 0x%04X，收到 %s" % (w, "".join(str(x) for x in slots)))
            break
    else:
        report("稳态段 %d 个半帧的 32 时隙位模式全部正确" % checked, checked >= 6,
               "逐半帧核对：时隙 0 = 0，1..16 = bit15..bit0，17..31 = 0")

    # ------------------------------------------------------------
    #  [3] STM32 解出的样本 vs FPGA 装载的样本
    # ------------------------------------------------------------
    print("\n[3] STM32 解码结果 vs FPGA 装载样本")
    loads, words = mirror.loads, mcu.words
    report("装载 / 解码数量配对（每半帧各一次）",
           abs(len(loads) - len(words)) <= 1,
           "装载 %d 次 / 解出 %d 个样本" % (len(loads), len(words)))
    m = min(len(loads), len(words))
    bad = [(i, loads[i][1], loads[i][2], words[i][1], words[i][2])
           for i in range(m)
           if loads[i][1] != words[i][1] or loads[i][2] != words[i][2]]
    report("逐样本比对：%d 个样本通道与数值全部一致" % m, not bad,
           ("不一致：%s" % bad[:3]) if bad else "")

    # 声道交替：任意相邻两个样本必须一左一右
    seq = [c for (_, c, _) in words]
    alt = all(seq[i] != seq[i + 1] for i in range(len(seq) - 1))
    report("左右声道严格交替（无串帧 / 无重复声道）", alt and len(seq) > 6,
           "声道序列 %s" % "".join("L" if c == 0 else "R" for c in seq[:12]))

    # ------------------------------------------------------------
    #  [4] 稳态下解出的字 == 麦克风进来的字（端到端闭环）
    # ------------------------------------------------------------
    print("\n[4] 端到端：STM32 解出的字 == codec 送进来的字（允许固定管线延迟）")

    def cyclic(decoded, src):
        """decoded 应当是 src 的某个循环连续段（跳过前 2 个预热样本）"""
        d = decoded[2:]
        if len(d) < 4:
            return False, "样本太少"
        j = src.index(d[0]) if d[0] in src else -1
        if j < 0:
            return False, "首字 0x%04X 不在源序列里" % d[0]
        for k, v in enumerate(d):
            if v != src[(j + k) % len(src)]:
                return False, "第 %d 个：解出 0x%04X，期望 0x%04X" % (
                    k, v, src[(j + k) % len(src)])
        return True, "偏移 %d，%d 个样本全对" % (j, len(d))

    dl = [w for (_, c, w) in words if c == 0]
    dr = [w for (_, c, w) in words if c == 1]
    gl, ml = cyclic(dl, LEFT_WORDS)
    gr, mr = cyclic(dr, RIGHT_WORDS)
    report("左声道：解出的字序列 == 输入字序列（循环）", gl, ml)
    report("右声道：解出的字序列 == 输入字序列（循环）", gr, mr)

    # ------------------------------------------------------------
    #  [5] 建立 / 保持裕量
    # ------------------------------------------------------------
    print("\n[5] 时序裕量：mcu_dout 翻转点离 mcu_bclk 上升沿的距离")
    chg, rise = r["dout_change_t"], r["bclk_rise_t"]
    if chg and rise:
        import bisect
        worst = 10 ** 9
        for rt in rise:
            i = bisect.bisect_left(chg, rt)
            for j in (i - 1, i):
                if 0 <= j < len(chg):
                    worst = min(worst, abs(rt - chg[j]))
        report("最小翻转-采样间距 >= 150 ns", worst >= 150,
               "实测最小 %d ns（BCLK 半周期 320 ns）" % worst)
        report("翻转次数与半帧数相称",
               len(chg) >= 2 * (2 * NFRAMES - 3),
               "翻转 %d 次 / 采样沿 %d 个" % (len(chg), len(rise)))
    else:
        report("采集到翻转与上升沿", False)

    print("\n" + "=" * 74)
    print(" 汇总：%s" % ("ALL PASS" if ok else "有 FAIL"))
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(check())
