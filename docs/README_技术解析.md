# FPGA → 单片机全 SPI 链路 + MCP4922 DAC + PAM8403 技术解析（无 I2S 方案）

> 配套接线见《README_接线指南.md》。本文回答"为什么这么连、为什么这么配"。
> DAC 已确认为 **MCP4922**（Microchip 双通道 12-bit SPI DAC，数据手册 DS21897A）；
> 本版按用户的《STM32F103C8T6引脚定义.xlsx》改为**全线 SPI、不用 I2S**。

## 0. 先澄清一个事实，再讲方案

你那张引脚定义表是**简化版**——它省掉了 F103 的 I2S/DAC 复用列。官方数据
手册（DS5319）里 F103C8T6 明确有 **I2S1（PA4/PA5/PA7）和 I2S2（PB12/13/15）**，
甚至还有双通道 12-bit 片内 DAC（PA4/PA5）。所以"没有 I2S"是表格的省略，
不是芯片的事实。

**但不用 I2S 完全可行且更简单**：I2S 是为 codec 之间连续音频流设计的，
而 FPGA→MCU 这一段本质上就是"每 20.5 µs 推两个 16 位数"——用 SPI 推更
直白：帧界清晰（CS̄ 一个字一拉）、不需要 16bit-extended 这种别扭的字长
组合、不需要 CHSIDE 门控。本方案全线只用 SPI：

| 段 | 总线 | STM32 角色 | 时钟谁给 |
|---|---|---|---|
| FPGA → STM32 | **SPI**（3 线） | SPI1 **从机**接收 | FPGA（新 RTL `spi_mirror_tx`） |
| STM32 → DAC | **SPI**（3+1 线） | SPI2 **主机** | STM32 自己给 SCK |

## 1. 整链架构与时钟树

```
FPGA 板晶振 50 MHz ──分频──► MCLK 12.5 MHz ──► ES8388（主模式 codec，板载）
                                                 │ BCLK 1.5625 MHz / LRCK 24.414 kHz
                                                 │ （这段 I2S 只在 FPGA 与板载 codec
                                                 │   之间，不出扩展口，与单片机无关）
        ┌─────────────────────────────────────────────────┐
        │ FPGA 变声处理 → tx_left / tx_right（16bit 立体声）│
        └─────────────────────────────────────────────────┘
                              │ spi_mirror_tx.v（新 RTL）
                              │ SPI 主机：SCLK 2 MHz、Mode 0,0
                              │ 每帧（40.96 µs）发两个字：先左后右，各配一个 CS̄
                              ▼
                 A13=SCLK / A14=CS̄ / A15=MOSI（GPIO0 排针）
                              ▼
                 STM32 SPI1 从机接收（PA5/PA4/PA7）
                              │ EXTI4（PA4 下降沿）同步左右声道
                              ▼
                 STM32 SPI2 主机（PB13/PB15/PB12=CS̄，PB0=LDAC̄）
                              ▼
                 MCP4922 → 两级 RC + 10µF 隔直 → PAM8403 → 2× 3525 喇叭
```

采样率由 ES8388 决定（fs = 24.414 kHz = MCLK/512）。FPGA→MCU 段的发送
节奏由"新样本就绪"事件驱动（与变声算法的装载时刻同源），SPI 从机被动
跟随，天然无漂移；MCU→DAC 段是主机推送，不存在漂移问题。

## 2. FPGA 侧：`spi_mirror_tx.v`（新 RTL，替代 `i2s_mirror_tx.v`）

| 项 | 设计 |
|---|---|
| 端口 | `mcu_sclk / mcu_nss / mcu_mosi`（+ `clk, rst_n, tx_left, tx_right`），引脚仍是 A13/A14/A15（改 xdc 端口名即可） |
| SCLK | 50 MHz ÷ 25 = **2 MHz**（计数器连续跑，字内使能输出） |
| 帧结构 | 每 40.96 µs（fs 周期）发两个字：word0 = 左声道，word1 = 右声道，各 16 bit，MSB 先行 |
| CS̄ | 每个字拉低 16 个 SCLK，字间保持高 ≥1 个 SCLK（满足 STM32 SPI 从机 NSS 帧定界） |
| 模式 | **Mode 0,0**：数据在 SCK 下降沿更新，上升沿被 STM32 采样（与 STM32 CPOL=0/CPHA=0 对应） |
| 同步 | 三级同步器结构沿用原 `i2s_mirror_tx.v` 的手法；输出 LVCMOS33、SLEW=SLOW（面包板飞线抗振铃） |

行为级骨架：

```verilog
// 每个字：nss<=0，计数 16 位，mosi <= word[15-cnt]（SCK 下降沿更新）
// 字结束：nss<=1；等帧事件（tx_left/tx_right 装载脉冲）再发下一对
// 帧事件来自变声通路已有的装载时刻，与 codec LRCK 同源
```

## 3. STM32 侧：SPI1 从机接收 + 左右声道同步

### 3.1 SPI1 从机配置

| 项目 | 值 | 说明 |
|---|---|---|
| 引脚 | PA5=SCK（输入）、PA4=NSS（输入）、PA7=MOSI（输入） | 全部是你表格"默认复用"栏里的功能 |
| 模式 | 从机、CPOL=0、CPHA=0、MSB 先行 | 与 FPGA 一致 |
| 帧长 | **16 bit**（CR1.DFF=1） | 一个字一次 RXNE，无需补零半字 |
| NSS | **硬件 NSS（SSM=0）** | ★ 这里和 I2S 方案相反：普通 SPI 从机下 NSS 低=被选中，FPGA 的 CS̄ 行为与之严丝合缝，不需要软件管理 |
| MISO | PA6 悬空 | 单向接收 |

对比 I2S 方案少了两个坑：没有 16bit-extended 的"每声道 1~2 次 RXNE"歧义，
也没有"I2S 模式下 NSS 脚被 WS 占用必须 SSM"的陷阱——CS̄ 低=收一个完整
16 位字，RXNE 一次=一个字，语义干净。

### 3.2 左右声道同步（EXTI4）

SPI 是无结构的位流，字本身不带"我是左还是右"的标签。解决：**PA4 接
EXTI4，CS̄ 下降沿中断里把声道标志复位为"左"**；随后第 1 次 RXNE 必是
左声道，第 2 次必是右声道。

- EXTI 频率 = 2 × fs ≈ 48.9 kHz，中断极轻。
- 即使某帧丢字导致声道错位，下一个 CS̄ 下降沿会**自动重新同步**——
  不会像纯计数方案那样一错到底。
- 串口打印 `ovr`（右声道到点时左字还没来的次数）恒为 0 即链路健康。

## 4. STM32 → MCP4922：SPI2 主机

### 4.1 SPI2 配置

| 项目 | 值 | 依据 |
|---|---|---|
| 引脚 | PB13=SCK、PB15=MOSI（复用推挽），**PB12=CS̄（GPIO 输出）**、PB0=LDAC̄（GPIO 输出） | SPI2 是主机，NSS 脚不当 NSS 用，改普通 IO |
| 模式 | **0,0**（CPOL=0，CPHA=0），MSB 先行 | DS21897A §5.1：数据在 SCK 上升沿锁入 |
| SCK 频率 | **4.5 MHz**（BR=fPCLK1/16，fPCLK1=36 MHz） | 芯片支持 20 MHz；面包板飞线留 4 倍余量 |
| 帧长 | 16 bit（DFF=1） | 写命令就是 16 bit |

### 4.2 16 位命令帧（DS21897A Register 5-1 原文）

| bit15 | bit14 | bit13 | bit12 | bit11..0 |
|---|---|---|---|---|
| A/B | BUF | GA | SHDN | D11..D0 |

- **A/B**：0 = 写 DACA，1 = 写 DACB
- **BUF**：0 = VREF 不缓冲（输入范围 0~VDD），1 = 缓冲
- **GA**：**1 = 1× 增益（VOUT = VREF × D/4096），0 = 2× 增益**
- **SHDN**：1 = 输出使能，0 = 输出高阻

**本工程固定 `0x3000 | D`（DACA）/ `0xB000 | D`（DACB）**：非缓冲 +
**1× 增益** + 激活。1× 是硬要求——VREF=VDD(3.3V) 时若误用 2×，代码过半
就顶到电源轨，**音频上半程全削平**。输出：静音（D=2048）时 1.65 V 直流，
满幅 0~3.3 V。

### 4.3 LDAC 双通道同步

MCP4922 内部双缓冲：CS̄ 上升沿只锁进**输入寄存器**，LDAC̄ 拉低才转移到
**输出寄存器**。流程：写 DACA 帧 → 写 DACB 帧 → PB0 拉低 ≥100 ns → 拉高。
两路输出同一时刻更新，左右零相差。（LDAC̄ 直接接地也能响，代价是左右
更新错开 20.5 µs；PB0 一根线别省。）

### 4.4 时序预算

| 事件 | 周期 | 耗时 |
|---|---|---|
| FPGA 字到达（每字） | 20.5 µs | SPI1 RXNE ISR ≈ 2 µs |
| DAC 写（每声道） | — | 16 bit @4.5 MHz = 3.6 µs + CS/LDAC 开销 |
| CPU 占用 | — | **<5%**，无需 DMA |

### 4.5 样本转换与固件骨架（寄存器级，风格同 mcu/i2s_stereo_dac.c）

```c
/* 16bit 有符号样本 → 12bit 无符号：+32768 抬到 0 基准，再 >>4 截位 */
static inline uint16_t to_dac12(int16_t s){ return (uint16_t)((s + 32768) >> 4); }

/* ---- SPI1 从机（收 FPGA）：CR1 = 从机|CPOL0|CPHA0|DFF16，SSM=0，SPE=1 ---- */
/* ---- SPI2 主机（发 DAC）：CR1 = MSTR|BR=fPCLK1/16|CPOL0|CPHA0|DFF16|SSM|SSI，SPE=1 ---- */

/* EXTI4（PA4 下降沿）：side = LEFT */
/* SPI1 RXNE 中断：*/
void spi1_rx_isr(void) {
    uint16_t w = SPI1->DR;                 /* 16 位一整字，读 DR 自动清 RXNE */
    if (side == LEFT) { side = RIGHT; g_left = w; }
    else { side = LEFT;  g_right = w; dac_flush(); }
}
void dac_flush(void) {                     /* 左右都到齐，写 DAC 并同步锁存 */
    dac_write(0x3000u, to_dac12(g_left));  /* DACA：非缓冲/1x/激活 */
    dac_write(0xB000u, to_dac12(g_right)); /* DACB */
    GPIOB->BSRR = GPIO_BSRR_BR0;           /* LDAC=0 */
    __NOP();__NOP();__NOP();__NOP();__NOP();__NOP();__NOP();__NOP();  /* ≥100 ns */
    GPIOB->BSRR = GPIO_BSRR_BS0;           /* LDAC=1，双通道同拍输出 */
}
void dac_write(uint16_t cmd, uint16_t d) {
    while (SPI2->SR & SPI_SR_BSY);
    GPIOB->BSRR = GPIO_BSRR_BR12;          /* CS=0 */
    SPI2->DR = cmd | (d & 0x0FFF);
    while (!(SPI2->SR & SPI_SR_TXE));
    while (SPI2->SR & SPI_SR_BSY);
    GPIOB->BSRR = GPIO_BSRR_BS12;          /* CS=1，锁入输入寄存器 */
}
```

原 `mcu/i2s_stereo_dac.c` 的串口诊断（PA9/PA10，`fs=`/`ovr=` 打印）原样
保留，把 I2S2/片内 DAC 段换成上面两个 ISR 即可。

## 5. MCP4922 原理：电阻串 DAC（不是 Σ-Δ）

- **电阻串（resistive string）架构**：2^12 个电阻分压点直接"查表"输出，
  零转换延迟、DNL ±0.2 LSB typ；输出建立 4.5 µs ≪ 20.5 µs 更新周期。
- 输出是**零阶保持**阶梯波，镜像在 fs=24.4 kHz 及其整数倍 → 后级两级 RC
  低通（fc≈15 kHz，落在奈奎斯特 12.2 kHz 与 fs 之间）是必选件。
- VREF 接 VDD：输出与电源成比例（ratio-metric），3.3 V 轨纹波 1:1 进
  音频——VREFA/VREFB 各挂 100 nF；SHDN̄ 接 3.3V 禁止硬件关断。
- SPI 上限 20 MHz（DS21897A），设计用 4.5 MHz，4 倍裕量。
- 16→12 bit 量化损失 24 dB 动态（总约 72 dB），听人声无感，诚实告知。

## 6. PAM8403：D 类功放与增益预算

| 特性 | 值 |
|---|---|
| 架构 | D 类（PWM ~250 kHz）+ BTL：喇叭两端对地对称摆动，输出无需隔直电容 |
| 增益 | 固定 24 dB（≈15.8×），模块电位器输入分压调音量 |
| 输出 | 5 V / 4Ω 下 2×3 W（THD 10%），效率 >85%；3525 喇叭 4Ω/3W 是最佳负载点 |

**增益预算**：DAC 满幅 1.17 V RMS ×15.8 = 18.5 V RMS ≫ 功放 3 W@4Ω 的
3.5 V RMS 上限——满幅必削波。对策：电位器先最小再慢慢加；固件
`VOL_SHIFT=2`（-12 dB）打底。噪声纪律：喇叭线短且绞合、功放独立 5V ≥1A、
星型共地。

## 7. 延迟预算（端到端）

| 环节 | 延迟 |
|---|---|
| FPGA 变声处理 | 数 ms（取决于算法块长） |
| FPGA→STM32 SPI 传输 | 16 µs/帧 |
| DAC 写 + 建立 | 3.6 µs + 4.5 µs |
| RC 群延迟 | µs 级 |
| **合计** | **毫秒级**，讲唱应用无感 |

## 8. 已知限制与改进路径（诚实版）

1. **采样率 24.414 kHz**：带宽 ~12 kHz，音乐高频闷。改 `es8388_init_table.v`
   换 48 kHz 分频，同时重算预期 fs 与 RC 取值。
2. **12 bit 量化**（MCP4922 固有）：要更好换 16-bit DAC；当前器件满足人声。
3. **SPI1 从机丢字即串声道**：已用 EXTI4 逐帧重同步兜底；若飞线质量差
   仍偶发，可把 SCLK 降到 1 MHz（RTL 改分频系数一行）。
4. **FPGA 需要新 RTL**：`spi_mirror_tx.v` 是本方案唯一的新增硬件工作，
   比原 `i2s_mirror_tx.v` 简单（无 32-bit 时隙计数，就 16 位移位 ×2）。
   需要的话我可以直接把 RTL + 仿真写出来跑一遍。
5. **VREF=VDD 的电源抑制比=0 dB**：要求高就给 MCP4922 的 3.3V 加磁珠+LC，
   或换独立基准（REF3033 等）。
6. 本机无 ARM 编译器；寄存器值按老办法用 `tool/verify_mcu_decode.py` 的
   方法论静态断言 + 行为级仿真验证。MCP4922 命令字已对照 DS21897A
   Register 5-1 核实。

## 9. 与"I2S 方案"的对照（为什么改版后依然站得住）

| 维度 | I2S2 收发方案 | 本方案（全 SPI） |
|---|---|---|
| 芯片能力 | F103C8T6 有 I2S2（你表格省略了该列） | 不依赖 I2S，表格可查 |
| STM32 外设 | I2S2 从机 + I2S1 从机 | SPI1 从机 + SPI2 主机 |
| 字长配置 | 16bit-extended（含 RXNE 次数歧义） | 纯 16 bit，一次 RXNE 一个字 |
| NSS 处理 | SSM=1 强制软件片选（PB12=WS 陷阱） | 硬件 NSS 直配 FPGA CS̄ |
| 声道判定 | CHSIDE 跳变门控 | EXTI4 帧界重同步 |
| FPGA RTL | 复用 `i2s_mirror_tx.v` | 新写 `spi_mirror_tx.v`（更简单） |
| 引脚 | PB12/13/15 + PA4/5/7 | PA4/5/7 + PB12/13/15 + PB0 |

两方案引脚规模相同、固件复杂度相近；全 SPI 方案的每一步都能在你那张
引脚定义表里找到依据，调试时示波器看到的也是"标准 SPI 波形"，更好讲、
更好查。

## 10. 文件对照

| 文件 | 作用 |
|---|---|
| `rtl/i2s_mirror_tx.v` | 旧方案 FPGA 镜像发送（保留，本方案不再使用） |
| `rtl/spi_mirror_tx.v` | **新方案 FPGA SPI 发送（待新增，要点见 §2）** |
| `constr/voice_changer.xdc` | A13/A14/A15 引脚约束（改端口名即可） |
| `mcu/i2s_stereo_dac.c` | 旧方案固件（I2S2 接收路径的串口诊断骨架可复用） |
| `tool/sim_i2s_mirror.py` | 旧方案仿真（方法论可移植到 SPI 位流） |
| `tool/verify_mcu_decode.py` | 寄存器位定义/ISR 验证（26 项断言，方法沿用） |
