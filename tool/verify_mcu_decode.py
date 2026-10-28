# -*- coding: utf-8 -*-
"""
verify_mcu_decode.py   验证 mcu/i2s_stereo_dac.c 里最可能写错的三个地方

本机没有 ARM 编译器，所以不做编译检查，改成"把 C 里的关键逻辑移植到 Python，
喂真实波形" —— 比语法检查有意义得多。验三件事：

  [1] 位定义：从 C 源码里解析出 I2SCFGR 的各个位，拼出实际写入寄存器的值，
      断言它等于 (I2SMOD | I2SCFG=从机接收 | Philips | 16bit数据/32bit通道)
      = 0x0901，且低两位 = 0x0001 == 标准库宏 I2S_DataFormat_16bextended。
      这一项专门防"位位置写错"——手册和库对 DATLEN/CHLEN 的说法不一致，
      两个信源核对过才敢写死。

  [2] ISR 解码：把 C 的 SPI2_IRQHandler 逐行移植过来，喂 sim_i2s_mirror.py
      产生的真实 mcu_bclk / mcu_lrck / mcu_dout 波形。
      RM0008 没说死 16bit-extended 是"每声道 1 次 RXNE"还是"2 次"，
      所以两种硬件行为都测，断言：
        · 两种模式下解出的左右样本都正确（CHSIDE 跳变门控的收益）
        · 2 次模式下补零半字全部被丢掉，一个有效样本都没漏
        · 作为对照：不做门控的朴素 ISR 在 2 次模式下必然错（负对照）

  [3] DAC 打包：s16 -> 12bit 无符号的换算，以及 DHR12RD 双通道打包位
      （低 12 位 = CH1/PA4/左，[27:16] = CH2/PA5/右），与独立实现逐一比对。

运行：python tool/verify_mcu_decode.py
"""

import ast
import operator
import os
import re
import sys

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOP)
sys.path.insert(0, os.path.join(TOP, "tool"))

import sim_i2s_mirror as SIM          # 复用镜像总线的波形生成

C_FILE = os.path.join(TOP, "mcu", "i2s_stereo_dac.c")

# 标准库 stm32f10x_spi.h 里的值（第二个信源）
SPL_I2S_DATaFormat_16bextended = 0x0001

# 期待拼出来的 I2SCFGR
EXPECT_I2SCFGR = (1 << 11) | (1 << 8) | (1 << 0)    # I2SMOD | 从机接收 | CHLEN=1
EXPECT_SPI_CR1 = (1 << 9)                            # 只设 SSM
EXPECT_SPI_CR2 = (1 << 6) | (1 << 5)                # RXNEIE | ERRIE

OK = True


def report(name, good, detail=""):
    global OK
    OK &= bool(good)
    print("  [%s] %s%s" % ("PASS" if good else "FAIL", name,
                           ("\n         " + detail) if detail else ""))


# ------------------------------------------------------------------
#  [1] 解析 C 源码里的宏定义，拼出真实的寄存器值
# ------------------------------------------------------------------

def parse_defines(path):
    """抓形如 `#define NAME  value` 的宏；value 只允许数字、括号、移位和已定义的宏名。

    函数式宏（`#define NAME(x) ...`）跳过 —— 判据是宏名后面紧跟左括号，
    不能拿"值里有没有括号"来判断，否则 `(1u << 11)` 这种正常宏会被误跳。
    """
    raw = {}
    with open(path, "r", encoding="utf-8") as fh:
        txt = fh.read()
    # 先剥掉注释，否则行尾的 /* 说明 */ 会被当成宏值的一部分。
    # 注意块注释可能跨行，所以先删块注释再删行注释。
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"//[^\n]*", "", txt)
    # 函数式宏的判据是"宏名后**紧跟**左括号"，中间不能允许空白，
    # 否则 `#define NAME  (1u << 11)` 会被误判成函数式宏。
    for m in re.finditer(r"^#define\s+([A-Za-z_][A-Za-z_0-9]*)(\()?", txt, re.M):
        name = m.group(1)
        if m.group(2):                      # 函数式宏，跳过
            continue
        rest = txt[m.end():].split("\n", 1)[0].strip()
        if rest:
            raw[name] = rest

    # C 宏里的整数除法：向零取整，和 Python 的 //（向下取整）不同
    def cdiv(a, b):
        if b == 0:
            raise ValueError("除零")
        q = abs(a) // abs(b)
        return q if (a < 0) == (b < 0) else -q

    _BIN = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.FloorDiv: cdiv, ast.Div: cdiv, ast.Mod: operator.mod,
        ast.LShift: operator.lshift, ast.RShift: operator.rshift,
        ast.BitOr: operator.or_, ast.BitAnd: operator.and_,
        ast.BitXor: operator.xor,
    }
    _UN = {ast.UAdd: operator.pos, ast.USub: operator.neg,
           ast.Invert: operator.invert}

    def _eval_ast(node):
        """只许整数常量和算术/位运算的 ast 白名单求值。

        为什么换掉 eval()：原来的字符白名单里含 `*`，于是 `2**99999999`
        是一个"合法"表达式，会让脚本卡在天文数字的幂运算上（DoS）；
        而且任何构建脚本里出现 eval 本身就是安全红线。ast 白名单里
        没有 Pow，遇到就明确报错。
        """
        if isinstance(node, ast.Expression):
            return _eval_ast(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int):
                raise ValueError("非整数常量：%r" % (node.value,))
            return node.value
        if isinstance(node, ast.BinOp):
            fn = _BIN.get(type(node.op))
            if fn is None:
                raise ValueError("不允许的运算符：%s"
                                 % type(node.op).__name__)
            return fn(_eval_ast(node.left), _eval_ast(node.right))
        if isinstance(node, ast.UnaryOp):
            fn = _UN.get(type(node.op))
            if fn is None:
                raise ValueError("不允许的运算符：%s"
                                 % type(node.op).__name__)
            return fn(_eval_ast(node.operand))
        raise ValueError("不允许的语法：%s" % type(node).__name__)

    def ev(expr, depth=0):
        if depth > 12:
            raise ValueError("递归太深：" + expr)
        expr = re.sub(r"([0-9a-fA-F])[uU]\b", r"\1", expr)   # 去掉 u/U 后缀
        # 去掉 C 风格的类型转换 (uint16_t)0x0800，ast 只认纯算术
        expr = re.sub(r"\(\s*(?:unsigned\s+|signed\s+)?"
                      r"(?:u?int\d+_t|u?int|char|short|long)\s*\)", "", expr)
        for k in sorted(raw, key=len, reverse=True):
            if k != expr and re.search(r"\b%s\b" % re.escape(k), expr):
                expr = re.sub(r"\b%s\b" % re.escape(k),
                              "(%d)" % ev(raw[k], depth + 1), expr)
        expr = expr.strip()
        if not expr:
            raise ValueError("空表达式")
        return int(_eval_ast(ast.parse(expr, mode="eval")))

    out = {}
    for k in raw:
        try:
            out[k] = ev(raw[k])
        except Exception:
            pass
    return out


def check_bitdefs():
    print("=" * 74)
    print(" mcu/i2s_stereo_dac.c —— 位定义 / ISR 解码 / DAC 打包 验证")
    print("=" * 74)

    if not os.path.isfile(C_FILE):
        report("C 源码存在", False, C_FILE)
        return None
    d = parse_defines(C_FILE)
    need = ["I2S_I2SMOD", "I2S_I2SE", "I2S_I2SCFG_SLAVERX",
            "I2S_I2SSTD_PHILIPS", "I2S_CKPOL_LOW", "I2S_DATLEN_16",
            "I2S_CHLEN_32", "SPI_CR1_SSM", "SPI_CR2_RXNEIE", "SPI_CR2_ERRIE",
            "SPI_SR_RXNE", "SPI_SR_CHSIDE", "SPI_SR_OVR",
            "DAC_CR_EN1", "DAC_CR_EN2"]
    miss = [n for n in need if n not in d]
    report("C 源码里能解析出全部关键位定义", not miss,
           ("缺少：%s" % miss) if miss else "%d 个" % len(need))
    if miss:
        # 原来只记一条 FAIL 就往下走，接着 d["I2S_I2SMOD"] 直接 KeyError，
        # 后面的 ISR / DAC 检查全都不跑，脚本以 traceback 收场而不是"有 FAIL"
        print("  [跳过] 位定义不全，后续寄存器拼装检查无法进行")
        return None

    print("\n[1] I2SCFGR 位定义（防位位置写错）")
    report("I2SMOD    = bit 11 = 0x%03X" % (1 << 11), d.get("I2S_I2SMOD") == (1 << 11),
           "实际 0x%03X" % d.get("I2S_I2SMOD", -1))
    report("I2SE      = bit 10 = 0x%03X" % (1 << 10), d.get("I2S_I2SE") == (1 << 10),
           "实际 0x%03X" % d.get("I2S_I2SE", -1))
    report("I2SCFG 从机接收 = bit 8 = 0x%03X" % (1 << 8),
           d.get("I2S_I2SCFG_SLAVERX") == (1 << 8),
           "实际 0x%03X" % d.get("I2S_I2SCFG_SLAVERX", -1))
    report("CHLEN(32位通道) = bit 0      ", d.get("I2S_CHLEN_32") == (1 << 0),
           "实际 0x%03X" % d.get("I2S_CHLEN_32", -1))
    report("DATLEN(16位数据)= bits[2:1]=00", d.get("I2S_DATLEN_16") == 0,
           "实际 0x%03X" % d.get("I2S_DATLEN_16", -1))

    cfg = (d["I2S_I2SMOD"] | d["I2S_I2SCFG_SLAVERX"] | d["I2S_I2SSTD_PHILIPS"]
           | d["I2S_CKPOL_LOW"] | d["I2S_DATLEN_16"] | d["I2S_CHLEN_32"])
    report("拼出的 I2SCFGR = 0x%04X（期望 0x%04X）" % (cfg, EXPECT_I2SCFGR),
           cfg == EXPECT_I2SCFGR)
    fmt = cfg & 0x0007
    report("低 3 位 = 0x%X == I2S_DataFormat_16bextended" % fmt,
           fmt == SPL_I2S_DATaFormat_16bextended,
           "DATLEN=00 + CHLEN=1，与 stm32f10x_spi.h 的宏值一致")
    report("SPI_CR1 = 0x%04X（只设 SSM，SSI=0 使从机始终被选中）" % EXPECT_SPI_CR1,
           d.get("SPI_CR1_SSM") == EXPECT_SPI_CR1)
    report("SPI_CR2 = 0x%04X（RXNEIE | ERRIE）" % EXPECT_SPI_CR2,
           (d.get("SPI_CR2_RXNEIE", 0) | d.get("SPI_CR2_ERRIE", 0)) == EXPECT_SPI_CR2)
    report("CHSIDE 在 SR 的 bit 2", d.get("SPI_SR_CHSIDE") == (1 << 2),
           "实际 0x%02X" % d.get("SPI_SR_CHSIDE", -1))
    return d


# ------------------------------------------------------------------
#  [2] ISR 解码：把 C 的逻辑移植过来，喂真实波形
# ------------------------------------------------------------------

def s16_to_dac12(v, vol_shift=0):
    """mcu 里 s16_to_dac12() 的移植"""
    s = v - 65536 if v >= 32768 else v        # int16
    if vol_shift:
        s >>= vol_shift
    s += 32768
    s >>= 4
    return 0 if s < 0 else (4095 if s > 4095 else s)


def dac_pack(left12, right12):
    """DHR12RD：低 12 位 = CH1(PA4/左)，[27:16] = CH2(PA5/右)"""
    return ((right12 & 0xFFF) << 16) | (left12 & 0xFFF)


def run_isr(events, gate=True):
    """events: [(side, halfword), ...]，完全按 C 的 SPI2_IRQHandler 写。
    返回 (帧列表 [(l, r, dhr)], dummy 数)"""
    last_side = -1
    left = 0
    frames = []
    dummy = 0
    for side, v in events:
        if side != last_side or not gate:
            if gate:
                last_side = side
            if side == 0:
                left = v
            else:
                frames.append((left, v, dac_pack(s16_to_dac12(left),
                                                 s16_to_dac12(v))))
        else:
            dummy += 1
    return frames, dummy


def make_events(half_frames, rxne_per_ch):
    """把 {半帧: 32 个时隙} 变成 ISR 看到的 (side, halfword) 事件流。

    rxne_per_ch = 1：每声道一次 RXNE，给出有效数据半字
    rxne_per_ch = 2：每声道两次，第二次是 16bit-extended 的补零半字

    ★ 补零半字直接取 0，不从时隙里拼：RM0008 原话是 "The 16 remaining bits
      are forced by hardware to 0x0000 to extend the data to 32-bit format"。
      Philips 标准下线上实际是 1 个无效位 + 16 数据位 + 15 个补零位（共 32），
      但 I2S 单元把它当成一个完整的 0x0000 半字交给软件，
      所以这里按手册建模，不去抠那 15/16 的差异。
    """
    ev = []
    for ch, slots in half_frames:
        if len(slots) != 32:
            continue
        w = 0
        for i in range(16):
            w = (w << 1) | slots[1 + i]        # 时隙 1..16 = bit15..bit0
        ev.append((ch, w))
        if rxne_per_ch == 2:
            ev.append((ch, 0))                 # 硬件强制的 0x0000 扩展字段
    return ev


def check_isr():
    print("\n[2] ISR 解码（喂 sim_i2s_mirror 产生的真实镜像波形）")

    r = SIM.sim()
    hf = r["mcu"].half_frames[:-1]        # 去掉末尾可能残缺的半帧
    loads = r["mirror"].loads[:len(hf)]   # 与之对应的 FPGA 装载记录

    # 仿真是半路起振的，第一个完整半帧可能是右声道。
    # 真实硬件上如果从机恰好在右半帧开始收，第一帧的左声道会是无效值，
    # 属正常的上电暂态。这里裁掉开头的非左半帧再比对（C 代码里 g_left
    # 初始化成静音电平 0x8000，所以那一帧只是"左声道静音"，不会炸音）。
    k0 = 0
    while k0 < len(hf) and hf[k0][0] != 0:
        k0 += 1
    hf = hf[k0:]
    loads = loads[k0:]

    # 真值：相邻的（左装载, 右装载）就是一帧立体声
    exp = []
    i = 0
    while i + 1 < len(loads):
        if loads[i][1] == 0 and loads[i + 1][1] == 1:
            exp.append((loads[i][2], loads[i + 1][2]))
        i += 1

    for per in (1, 2):
        ev = make_events(hf, per)
        frames, dummy = run_isr(ev)

        n = min(len(frames), len(exp))
        bad = [(k, frames[k][0], frames[k][1], exp[k][0], exp[k][1])
               for k in range(n)
               if frames[k][0] != exp[k][0] or frames[k][1] != exp[k][1]]
        report("每声道 %d 次 RXNE：%d 帧解出的样本与 FPGA 装载一致"
               % (per, n), not bad and n >= 6,
               ("不一致：%s" % bad[:3]) if bad else
               "解出 %d 帧 / 期望 %d 帧" % (len(frames), len(exp)))
        if per == 2:
            report("每声道 2 次：补零半字被全部丢弃（dummy = %d）" % dummy,
                   dummy == len(hf),
                   "完整半帧 %d 个，丢掉的补零半字 %d 个" % (len(hf), dummy))

    # ---- 负对照：不做 CHSIDE 门控，2 次 RXNE 时必然错 ----
    ev2 = make_events(hf, 2)
    naive, _ = run_isr(ev2, gate=False)
    naive_bad = sum(1 for k in range(min(len(naive), len(exp)))
                    if naive[k][0] != exp[k][0] or naive[k][1] != exp[k][1])
    report("负对照：不做门控时每声道 2 次必然解错（证明门控不是多余的）",
           naive_bad > 0,
           "朴素 ISR 有 %d/%d 帧解错" % (naive_bad, min(len(naive), len(exp))))


# ------------------------------------------------------------------
#  [3] DAC 量化与打包
# ------------------------------------------------------------------

def check_dac():
    print("\n[3] DAC 量化与双通道打包")

    #  0x0000 ->     0 + 32768 = 32768 -> >>4 = 2048  （静音 = Vref/2）
    #  0x7FFF -> 32767 + 32768 = 65535 -> >>4 = 4095  （正满幅，饱和到 4095）
    #  0x8000 -> -32768+ 32768 =     0 -> >>4 =    0  （负满幅）
    #  0x0100 ->   256 + 32768 = 33024 -> >>4 = 2064
    #  0xFF00 ->  -256 + 32768 = 32512 -> >>4 = 2032
    cases = [(0x0000, 2048), (0x7FFF, 4095), (0x8000, 0),
             (0x0100, 2064), (0xFF00, 2032), (0x7FF0, 4095),
             (0x0001, 2048), (0xFFFF, 2047)]
    bad = [(hex(a), s16_to_dac12(a), b) for a, b in cases if s16_to_dac12(a) != b]
    report("s16 -> 12bit 无符号（0x0000->2048, 0x7FFF->4095, 0x8000->0）",
           not bad, ("错误：%s" % bad) if bad else
           "静音 0x0000 -> 2048（= Vref/2），正负满幅到 4095 / 0")

    # 打包
    l, r = 0x1234, 0xEDCB
    v = dac_pack(s16_to_dac12(l), s16_to_dac12(r))
    ch1 = v & 0xFFF
    ch2 = (v >> 16) & 0xFFF
    report("DHR12RD 打包：CH1(PA4)=左、CH2(PA5)=右",
           ch1 == s16_to_dac12(l) and ch2 == s16_to_dac12(r),
           "写入 0x%08X -> CH1=%d（左 0x%04X）、CH2=%d（右 0x%04X）"
           % (v, ch1, l, ch2, r))

    # 满量程不溢出 12 位
    v = dac_pack(s16_to_dac12(0x8000), s16_to_dac12(0x7FFF))
    report("满量程打包不越界（<= 0x0FFF0FFF）", v <= 0x0FFF0FFF,
           "实际 0x%08X" % v)


# ------------------------------------------------------------------

def check_source_sanity():
    """C 文件的基本静态检查：括号配平 + 关键函数都在"""
    txt = open(C_FILE, "r", encoding="utf-8").read()
    # 粗略去掉注释与字符串
    t = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    t = re.sub(r"//[^\n]*", "", t)
    t = re.sub(r'"(\\.|[^"\\])*"', '""', t)
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for ch_ in t:
        if ch_ in pairs:
            stack.append(ch_)
        elif ch_ in pairs.values():
            if not stack or pairs[stack.pop()] != ch_:
                break
    report("C 源码括号配平", not stack,
           ("未闭合：%s" % stack[:5]) if stack else "")

    for fn in ("SPI2_IRQHandler", "SysTick_Handler", "main",
               "i2s2_slave_rx_init", "dac_init", "clock_init", "gpio_init"):
        report("已定义 %s()" % fn, re.search(r"\b%s\s*\(\s*void\s*\)\s*\{" % fn, t)
               or re.search(r"\b%s\s*\(\s*\)\s*\{" % fn, t))


if __name__ == "__main__":
    check_source_sanity()
    check_bitdefs()
    check_isr()
    check_dac()
    print("\n" + "=" * 74)
    print(" 汇总：%s" % ("ALL PASS" if OK else "有 FAIL"))
    print("=" * 74)
    raise SystemExit(0 if OK else 1)
