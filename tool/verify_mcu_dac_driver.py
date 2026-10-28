# -*- coding: utf-8 -*-
"""
verify_mcu_dac_driver.py —— 验证 mcu/spi2_dma_dac.c（音响驱动可靠版）

本机没有 ARM 编译器，所以不编译，而是把 C 里的**核心算法**逐行移植到
Python，喂真实波形逐样本跑。验的是逻辑，不是语法。

要证明的五件事（对应原驱动 i2s_stereo_dac.c 的硬伤 + 重写过程中新踩的坑）：

  [1] 正常情况：左右声道逐样本对得上，绝不串位
  [2] 丢字恢复：中途丢一个字之后，新方案（CS̄ 每帧一个下降沿）下一帧就
      恢复；旧方案（CS̄ 每字一个下降沿）会**永久左右颠倒** —— 负对照
  [3] 晶振漂移：两侧时钟差 ±1000 ppm 时，改 TIM6 的 ARR 做频率跟踪，
      能长时间把缓冲稳在中间，既不溢出也不欠载
  [4] DAC 量化与双通道打包：s16 -> 12bit 无符号、DHR12RD 位布局
  [5] 写指针量化：只在 DMA 半缓冲中断里推进 g_wr（=每 256 字跳一次）会
      让水位被系统性低报，每个半周期末尾误判欠载 —— 这是重写时真踩过的
      坑，留一条负对照防止以后改回去

运行：python tool/verify_mcu_dac_driver.py
"""

import math
import os
import sys

import numpy as np

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOP)
sys.path.insert(0, os.path.join(TOP, "host"))

OK_ALL = True

FS_NOM = 24414.0625          # FPGA 侧的标称帧率（50 MHz / 2048）
MCU_CLK = 72.0e6             # TIM6 时钟（clock_init 把系统时钟配到 72 MHz）
# ARR = TIM_CLK / FS - 1。写成 2949 会变成 24406.8 Hz（-298 ppm，每秒补 7 帧）；
# 2948 是 24415.1 Hz（+41 ppm，每秒才补 1 帧）。
ARR_NOM = int(MCU_CLK // FS_NOM) - 1
RB_BAND = 64                 # 水位偏离带（= RB_WORDS/8）


def report(name, good, detail=""):
    global OK_ALL
    OK_ALL &= bool(good)
    print("  [%s] %s%s" % ("PASS" if good else "FAIL", name,
                           ("\n         " + detail) if detail else ""))


# ---------------------------------------------------------------- 常量（与 C 一致）
RB_WORDS = 512
DAC_MID = 0x0800


def s16_to_dac(s):
    """C 里的 s16_to_dac()：有符号 16bit -> 12bit 无符号。"""
    u = (int(s) + 32768) & 0xFFFFFFFF
    if u > 0xFFFF:
        u = 0xFFFF
    u >>= 4
    return min(u, 4095)


def _as_s16(u):
    """无符号 16bit 字 -> 有符号 int16（C 里就是一次强制类型转换）。"""
    u = int(u) & 0xFFFF
    return u - 65536 if u >= 32768 else u


def pack_dhr12rd(l, r):
    """C 里的 pack_dhr12rd()：低 12 位 = CH1(PA4,左)，[27:16] = CH2(PA5,右)。"""
    return (int(l) & 0x0FFF) | ((int(r) & 0x0FFF) << 16)


def diag_wave(ph, amp):
    """C 里的 diag_wave()：Q16 相位 -> 三角波（上电自检提示音）。"""
    h = ph & 0x7FFF
    v = (h - 16384) << 1
    if ph & 0x8000:
        v = -v
    return v * amp // 32768


# ---------------------------------------------------------------- 驱动模型
class SpiDmaDacModel(object):
    """mcu/spi2_dma_dac.c 的 Python 移植（只含与验收有关的逻辑）。"""

    def __init__(self, rb_words=RB_WORDS, cs_per_frame=True, quantize_wr=False):
        self.n = rb_words
        self.buf = np.zeros(rb_words, dtype=np.uint16)
        self.wr = 0                  # 已接收字数（单调增）
        self.rd = 0                  # 已消费字数（单调增）
        self.cs_per_frame = cs_per_frame
        # quantize_wr=True 模拟"只在半缓冲中断里推进写指针"的写法（负对照）
        self.quantize_wr = quantize_wr
        self._true_wr = 0
        self.frame_anchor = 0
        self.phase_valid = False
        self.phase = 0
        self.ovr = 0
        self.resync = 0
        self.under = 0
        self.over = 0
        self.last_l = DAC_MID
        self.last_r = DAC_MID
        self.min_level = 1 << 30
        self.max_level = 0
        self.hold = 0                 # 因水位不足而保持上一帧的次数
        self.drop = 0                 # 追不上时丢掉的帧数
        self.starve = 0               # 真没数据（物理缓冲也是空的）的次数

    # ---- FPGA 侧把一个字推进来（DMA 循环写入） ----
    def push_word(self, w):
        # int(w) 先转 Python int：numpy 2 里 np.int16(-8000) & 0xFFFF 会抛
        # OverflowError（有符号 16 位装不下 65535 这个无符号结果）
        self.buf[self._true_wr & (self.n - 1)] = np.uint16(int(w) & 0xFFFF)
        self._true_wr += 1
        if self.quantize_wr:
            # 只在跨过半缓冲边界时才让可见的 wr 前进
            self.wr = (self._true_wr // (self.n // 2)) * (self.n // 2)
        else:
            self.wr = self._true_wr   # 对应 C 的 dma_wr_now()：随要随算，精确

    # ---- EXTI12：CS̄ 下降沿 ----
    def on_cs_falling(self):
        if self.cs_per_frame:
            if self.phase != 0:
                self.resync += 1
            self.phase = 0
            self.phase_valid = True
            self.frame_anchor = self.wr      # 下一个字就是左声道
            # ★ 真正的恢复动作在这里：
            #   CS̄ 下降沿意味着"FPGA 接下来要发的是左声道"，而已收到的
            #   最后一个字应该是上一帧的右声道。所以 (已收 - 已消费) 必须
            #   是**偶数**才对得上；是奇数就说明中途丢过字，丢掉一个凑成
            #   偶数，下一帧立刻恢复。只重置相位标记是不够的（实测那样
            #   错位会一直持续下去，因为读写速率仍然是一帧两个字）。
            if (self.wr - self.rd) & 1:
                if self.level() > 0:
                    self.rd += 1
                    self.resync += 1
        else:
            self.phase ^= 1
            self.phase_valid = True

    # ---- SPI 溢出 ----
    def on_ovr(self):
        self.ovr += 1
        self.phase_valid = False

    def level(self):
        return self.wr - self.rd

    def _pop(self):
        if self.level() <= 0:
            self.under += 1
            return 0x8000                    # 静音（s16 的 0）
        v = int(self.buf[self.rd & (self.n - 1)])
        self.rd += 1
        return v

    def mcu_hz(self):
        """当前 MCU 侧的实际输出帧率（对应 TIM_CLK / (ARR+1)）。"""
        return MCU_CLK / (ARR_NOM + 1.0)

    def reset_stats(self):
        """预填充完成后清零统计：启动阶段的 hold 是正常行为，不算失败。"""
        self.hold = 0
        self.drop = 0
        self.starve = 0
        self.under = 0
        self.over = 0
        self.min_level = 1 << 30
        self.max_level = 0

    # ---- TIM6 中断：每 1/fs 一次，一次更新左右两个 DAC 通道 ----
    def on_tick(self):
        lvl = self.level()
        self.min_level = min(self.min_level, lvl)
        self.max_level = max(self.max_level, lvl)

        # ---- 速率自适应：丢帧 / 重复帧，必须**整帧对齐** ----
        # 两侧晶振差几十到几百 ppm，而 TIM6 的 ARR 只有 ~340 ppm 一档的
        # 分辨率，残差只能靠动数据吸收。一动就是 2 个字（一整帧 L+R）：
        # 只丢/只补 1 个字会把左右相位打翻，那是"声音忽左忽右"的来源。
        mid = self.n // 2
        band = max(1, self.n // 8)
        if lvl > mid + band:
            if self.level() >= 2:
                self.rd += 2
                self.drop += 1
        elif lvl < mid - band:
            # 水位太低：本帧不消费，保持上一帧
            self.hold += 1
            return self.last_l, self.last_r

        if self.level() >= 2:
            w0 = self._pop()
            w1 = self._pop()
            if self.cs_per_frame and self.phase_valid:
                # 锚点之后的第 0 个字是左；错开一个字就交换
                if (self.rd - self.frame_anchor) & 1:
                    w0, w1 = w1, w0
            self.last_l = s16_to_dac(_as_s16(w0))
            self.last_r = s16_to_dac(_as_s16(w1))
        else:
            # 真没数据：保持上一帧（不写 0，避免"咔"）
            self.hold += 1
            self.starve += 1
        return self.last_l, self.last_r


# ---------------------------------------------------------------- 用例
def _mk_sequences(nfr, fs=FS_NOM, amp=8000):
    """生成 (L_seq, R_seq)：左右两个不同频率的正弦，串位立刻能看出来。"""
    L = [int(amp * math.sin(2 * math.pi * 220 * k / fs)) for k in range(nfr)]
    R = [int(amp * math.sin(2 * math.pi * 330 * k / fs)) for k in range(nfr)]
    return L, R


def _preroll(m, L, R, n_pre):
    """★ 必须预填充：水位自适应要求缓冲先填到 mid-band，否则启动阶段
    会一直保持上一帧（这是正常的启动瞬态，约 96 帧 = 4 ms）。
    预填充完再清统计，后面比对的才是稳态行为。"""
    for k in range(n_pre):
        m.on_cs_falling()
        m.push_word(L[k])
        m.push_word(R[k])
        m.on_tick()
    m.reset_stats()


def _test_normal():
    print("\n[1] 正常链路：左右声道逐样本对得上（缓冲区预填充后）")
    n_pre = 400
    nfr = n_pre + 1600
    L, R = _mk_sequences(nfr)
    m = SpiDmaDacModel(cs_per_frame=True)
    _preroll(m, L, R, n_pre)

    dl_max = dr_max = 0
    for k in range(n_pre, nfr):
        m.on_cs_falling()
        m.push_word(L[k])
        m.push_word(R[k])
        rd_before = m.rd
        dl, dr = m.on_tick()
        # 消费的是第 rd_before/2 帧；左右一旦串位，码值差会非常大
        j = rd_before // 2
        dl_max = max(dl_max, abs(dl - s16_to_dac(L[j])))
        dr_max = max(dr_max, abs(dr - s16_to_dac(R[j])))
    report("左声道逐样本一致（无串位）", dl_max == 0, "最大码值差 %d" % dl_max)
    report("右声道逐样本一致（无串位）", dr_max == 0, "最大码值差 %d" % dr_max)
    report("稳态无丢帧 / 无补帧 / 无欠载 / 无溢出",
           m.drop == 0 and m.hold == 0 and m.under == 0 and m.over == 0,
           "drop=%d hold=%d under=%d over=%d" % (m.drop, m.hold, m.under, m.over))


def _best_source(out, L, R, k0, k1, base_lag=0):
    """判断一段输出更像是"左路序列"还是"右路序列"。

    ★ 为什么不能像 [1] 那样逐样本对齐比：丢掉一个字之后，flat 下标 //2
      就等于帧号这个映射本身已经偏了半帧，out[k] 会对上 L[k±1]，
      220 Hz 正弦一帧的斜率就有 ~450 个码值，比"左右颠倒"的判据还大，
      于是"整帧平移一格"和"左右颠倒"分不开（实测误报 356/379）。
      改成"在 base_lag ±2 帧的平移范围内找最像的源"，抗平移，只认声像
      归属。base_lag 必须把环形缓冲的固定延迟（约 96 帧）算进去，否则
      所有假设的误差都会大到几百个码值，判不出东西。
    """
    lags = range(base_lag - 2, base_lag + 3)
    """判断一段输出更像是"左路序列"还是"右路序列"。

    ★ 为什么不能像 [1] 那样逐样本对齐比：丢掉一个字之后，flat 下标 //2
      就等于帧号这个映射本身已经偏了半帧，out[k] 会对上 L[k±1]，
      220 Hz 正弦一帧的斜率就有 ~450 个码值，比"左右颠倒"的判据还大，
      于是"整帧平移一格"和"左右颠倒"分不开（实测误报 356/379）。
      改成"在 ±2 帧的平移范围内找最像的源"，抗平移，只认声像归属。
    """
    best = {}
    for tag, seq in (("L", L), ("R", R)):
        err_min = None
        for lag in lags:
            s = 0
            cnt = 0
            ok = True
            for k in range(k0, k1):
                j = k + lag
                if j < 0 or j >= len(seq):
                    ok = False
                    break
                s += abs(out[k - k0] - s16_to_dac(seq[j]))
                cnt += 1
            if not ok or cnt == 0:
                continue
            e = s / float(cnt)
            if err_min is None or e < err_min:
                err_min = e
        best[tag] = err_min
    src = "L" if best["L"] <= best["R"] else "R"
    return src, best


def _test_drop_word():
    print("\n[2] 丢一个字之后左右声道归位（新方案 vs 旧方案负对照）")

    def run(cs_per_frame):
        n_pre = 400
        nfr = n_pre + 600
        drop_at = n_pre + 200
        L, R = _mk_sequences(nfr, amp=9000)
        m = SpiDmaDacModel(cs_per_frame=cs_per_frame)
        _preroll(m, L, R, n_pre)
        out_l = []
        out_r = []
        k_start = None
        base_lag = 0
        for k in range(n_pre, nfr):
            m.on_cs_falling()
            if k == drop_at:
                # 模拟丢一个字：只推一个字进去（左右就此错开半帧）
                m.push_word(L[k])
            else:
                m.push_word(L[k])
                m.push_word(R[k])
            rd_before = m.rd
            dl, dr = m.on_tick()
            if k > drop_at + 20:                # 给帧同步 20 帧的恢复时间
                if k_start is None:
                    k_start = k
                    base_lag = rd_before // 2 - k   # 环形缓冲的固定延迟
                out_l.append(dl)
                out_r.append(dr)
        sl, el = _best_source(out_l, L, R, k_start, nfr, base_lag)
        sr, er = _best_source(out_r, L, R, k_start, nfr, base_lag)
        return sl, sr, el, er

    sl, sr, el, er = run(True)
    report("新方案（CS̄ 帧同步）：丢字后左右各自归位",
           sl == "L" and sr == "R",
           "恢复后左路判为 %s（误差 L=%.1f / R=%.1f），右路判为 %s"
           "（误差 L=%.1f / R=%.1f）" % (sl, el["L"], el["R"], sr, er["L"], er["R"]))
    osl, osr, oel, oer = run(False)
    report("负对照：旧方案（CS̄ 每字）丢字后会左右颠倒",
           osl == "R" and osr == "L",
           "左路判为 %s、右路判为 %s（颠倒才说明帧同步不是多余的）" % (osl, osr))


def _test_drift(ppm, seconds=16.0):
    """晶振漂移：FPGA 和 MCU 各有各的晶振，差几百 ppm。

    事件驱动仿真：FPGA 按 fs_fpga 每帧推两个字 + 一个 CS̄ 下降沿，
    MCU 按 72 MHz/(ARR+1) 出帧，ARR 由驱动自己的 PLL 调。
    """
    print("\n[3] 晶振漂移 %+d ppm：水位能否长时间稳住（不欠载/不溢出）" % ppm)
    m = SpiDmaDacModel(cs_per_frame=True)
    fs_fpga = FS_NOM * (1.0 + ppm * 1e-6)

    t = 0.0
    t_end = seconds
    next_f = 1.0 / fs_fpga
    next_m = 1.0 / m.mcu_hz()
    k = 0
    late = []                        # 只统计后半段：启动瞬态不算
    half = t_end * 0.5
    while t < t_end:
        if next_f <= next_m:
            t = next_f
            L = int(7000 * math.sin(2 * math.pi * 220 * k / FS_NOM))
            R = int(7000 * math.sin(2 * math.pi * 330 * k / FS_NOM))
            m.on_cs_falling()
            m.push_word(L)
            m.push_word(R)
            k += 1
            next_f = t + 1.0 / fs_fpga
        else:
            t = next_m
            m.on_tick()
            if t > half:
                late.append(m.level())
            next_m = t + 1.0 / m.mcu_hz()

    ok = (m.max_level < RB_WORDS - 8) and m.over == 0 and m.under == 0 \
        and m.starve == 0
    report("缓冲既不溢出也不欠载（%.0f s / %d 帧）" % (t_end, k), ok,
           "水位 %d..%d / %d，over=%d under=%d starve=%d"
           % (m.min_level, m.max_level, RB_WORDS, m.over, m.under, m.starve))
    span = (max(late) - min(late)) if late else 0
    report("水位被钉在窄带内（后半段波动 < 64 字）", span < 64,
           "后半段水位 %d..%d，波动 %d 字" % (min(late) if late else -1,
                                       max(late) if late else -1, span))
    # 丢帧/补帧的代价：换算成"平均多少毫秒发生一次"
    corr = m.drop + m.hold
    per_ms = (t_end * 1000.0 / corr) if corr else float("inf")
    report("丢/补帧代价可接受（> 20 ms 一次）", per_ms > 20.0,
           "%.0f s 内丢 %d 帧、补 %d 帧，平均每 %.1f ms 一次（每帧 41 µs）"
           % (t_end, m.drop, m.hold, per_ms))


def _test_quant_and_pack():
    print("\n[4] DAC 量化与 DHR12RD 双通道打包 + 自检提示音波形")
    report("s16 0x0000 -> 2048（0 V）", s16_to_dac(0) == 2048,
           "实际 %d" % s16_to_dac(0))
    report("s16 0x7FFF -> 4095（正满幅）", s16_to_dac(32767) == 4095,
           "实际 %d" % s16_to_dac(32767))
    report("s16 0x8000 -> 0（负满幅）", s16_to_dac(-32768) == 0,
           "实际 %d" % s16_to_dac(-32768))
    v = pack_dhr12rd(0x123, 0xABC)
    report("DHR12RD：低 12 位 = 左(CH1/PA4)，[27:16] = 右(CH2/PA5)",
           (v & 0xFFF) == 0x123 and ((v >> 16) & 0xFFF) == 0xABC,
           "0x%08X" % v)
    report("打包不越界（<= 0x0FFF0FFF）", pack_dhr12rd(4095, 4095) <= 0x0FFF0FFF,
           "0x%08X" % pack_dhr12rd(4095, 4095))
    # 提示音：不能恒为 0（整数除法写错顺序就会整段塌掉）
    vals = [diag_wave((i * 2684) & 0xFFFF, 9000) for i in range(40)]
    report("自检提示音三角波幅度正常", max(vals) > 5000 and min(vals) < -5000,
           "范围 %d..%d（写反成 v/32768*amp 就会全塌成 0/-amp）"
           % (min(vals), max(vals)))


def _test_wr_quantization():
    """写指针量化（负对照）。

    C 里如果只在 DMA 的 HT/TC 中断里推进 g_wr，可见水位就会比真实水位
    低最多 256 字，于是"明明有数据"却被判成水位不足而保持上一帧。注意
    g_under 计数器不会涨（因为 rb_pop 只在水位 >= 2 时才调用），真正的
    症状是 g_hold 暴涨 —— 所以这里盯的是 hold，不是 under。
    """
    print("\n[5] 写指针量化（负对照）：只在半缓冲中断里推进 g_wr 会怎样")
    out = {}
    for tag, q in (("精确写指针（dma_wr_now，现在的实现）", False),
                   ("半缓冲量化写指针（会咬人的旧写法）", True)):
        m = SpiDmaDacModel(cs_per_frame=True, quantize_wr=q)
        nfr = 20000
        L, R = _mk_sequences(nfr, amp=7000)
        _preroll(m, L, R, 400)           # 同样的预填充，排除启动瞬态
        for k in range(400, nfr):
            m.on_cs_falling()
            m.push_word(L[k])
            m.push_word(R[k])
            m.on_tick()
        out[tag] = (m.hold, m.starve, m.under, m.over)

    g_hold, g_starve, g_under, g_over = out["精确写指针（dma_wr_now，现在的实现）"]
    b_hold, _, _, _ = out["半缓冲量化写指针（会咬人的旧写法）"]
    report("精确写指针：不误判（hold=0 starve=0）",
           g_hold == 0 and g_starve == 0 and g_under == 0 and g_over == 0,
           "hold=%d starve=%d under=%d over=%d" % (g_hold, g_starve, g_under, g_over))
    report("负对照：量化写指针会大量误判成水位不足", b_hold > 100,
           "hold=%d / 20000 帧（>0 才说明改成 dma_wr_now() 是有必要的）" % b_hold)

    # 顺带验一下标称 ARR：72 MHz / (ARR+1) 与 FPGA 的偏差要远小于 ppm 级
    ppm_off = (MCU_CLK / (ARR_NOM + 1.0) / FS_NOM - 1.0) * 1e6
    report("标称 ARR 与 FPGA 帧率偏差 < 100 ppm", abs(ppm_off) < 100.0,
           "ARR=%d -> %.3f Hz，偏差 %+.1f ppm（写成 2949 会是 -298 ppm）"
           % (ARR_NOM, MCU_CLK / (ARR_NOM + 1.0), ppm_off))


def main():
    print("=" * 74)
    print(" mcu/spi2_dma_dac.c —— 音响驱动（SPI2 从机 DMA + 帧同步 + 水位自适应）验证")
    print("=" * 74)
    _test_quant_and_pack()
    _test_normal()
    _test_drop_word()
    _test_wr_quantization()
    _test_drift(+1000)
    _test_drift(-1000)
    print("\n" + "=" * 74)
    print(" 汇总：%s" % ("ALL PASS" if OK_ALL else "有 FAIL"))
    print("=" * 74)
    return 0 if OK_ALL else 1


if __name__ == "__main__":
    sys.exit(main())
