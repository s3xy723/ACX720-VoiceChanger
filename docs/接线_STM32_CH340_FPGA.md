# STM32 / CH340 / FPGA 接线要求

> 对应 ACX720-V3 管脚分配表（2024-11-13）与 `ACX720-V3-SCH.pdf` 第 11 页 P7。
> 实物 P7 是 **2×20 排针**，引脚号以板子丝印/方形焊盘为准。

## 0. 先搞清楚：音频主链路是 SPI，不是串口

```
FPGA  --SPI1(16bit, 2.08MHz, 左/右字)-->  STM32  --SPI2-->  MCP4922  -->  PAM8403  -->  喇叭
          A13/A14/A15 -> PA5/PA4/PA7              PB13/PB15/PB12/PB0
```

- **FPGA→STM32 是 SPI**，走 P7 的 A13/A14/A15；这是音频数据主链路。
- **STM32→MCP4922 也是 SPI**，走 PB13/PB15/PB12/PB0。
- **PA9 的 UART / CH340 / COM4 只是调试日志**（每 500 ms 一行状态），
  **不参与音频传输**。CH340 不接，DAC 和喇叭照样能出声。
- 所以 COM4 收不到日志，不等于音频链路坏了；真正要看的是
  `g_words`（SPI1 收到的字数）和 DAC 输出。

## 1. 音频主链路：FPGA→STM32 SPI（必须）

P7 排针的 **A13/A14/A15 正好是 pin 1/2/3**：

| ACX720 P7 引脚 | FPGA 引脚 | 信号 | STM32 | 必须 |
|---|---|---|---|---|
| **pin 1** | A13 | `mcu_sclk` | **PA5**（SPI1_SCK） | ✅ |
| **pin 2** | A14 | `mcu_csn` | **PA4**（SPI1_NSS / EXTI4） | ✅ |
| **pin 3** | A15 | `mcu_mosi` | **PA7**（SPI1_MOSI） | ✅ |
| **pin 12 或 pin 30** | GND | GND | **GND** | ✅ |

P7 的电源脚（**不要当 GND 用**）：

| P7 引脚 | 网络 |
|---|---|
| pin 11 | **VCC5V0** |
| pin 12 | **GND** |
| pin 29 | **VCC3V3** |
| pin 30 | **GND** |

> 也就是说：**GND 用 pin 12 或 pin 30**，不要用 pin 11（5V）或 pin 29（3.3V）。
> 接错会把 5V/3.3V 灌进 GND 或把电源短路。

完整的 P7 引脚图见：
[`ACX720_GPIO0_P7_引脚图.png`](images/ACX720_GPIO0_P7_引脚图.png)

## 2. 调试串口：PA9→CH340（可选，不是音频链路）

| STM32F103C8T6 | CH340 模块 | 必须 | 说明 |
|---|---|---|---|
| **PA9**（USART1_TX，LQFP48 第 30 脚） | **RXD** | 调试时接 | STM32 发 → CH340 收 |
| **GND** | **GND** | ✅ | 必须共地 |
| PA10（USART1_RX，第 31 脚） | TXD | 可选 | 只有 PC 要发给 STM32 时才接 |

- 不要把 PA9 接 CH340 的 **TXD**（两个都是输出）。
- CH340 由 USB 供电时，VCC 不用接；模块有 3.3V/5V 跳线时选 3.3V。
- 不接 CH340 也能听声音；串口只影响“看状态日志”。

## 3. 电平与方向

- FPGA 的 GPIO0 bank 电压要设成 **3.3V**（板上 VCCIO 选择跳线）。
  分配表写的是 `3.3V/1.5V/1.8V/2.5V 可选`，如果跳在 1.8/2.5V，
  STM32 可能识别不到或边沿不可靠。
- SPI 是 **FPGA 主、STM32 从**：SCLK/CS/MOSI 三根都是 FPGA 输出、
  STM32 输入，不需要 MISO。
- 线长：SPI 三根 < 20 cm；UART < 1 m；所有板 **共地**。
- 先接 GND，再接信号，最后上电。

## 4. 接好后怎么验证

1. **先听声音**：不接 FPGA 时，STM32 固件会在 1 秒后输出 440 Hz 自检音
   （左右同相）。听得见 → STM32→MCP4922→PAM8403 这条链正常。
2. FPGA 送数据后，`g_words` 应持续增加，串口（若接了）显示：

```
mode=PASS words/s=48828 fs=24414 ovr=0
```

3. 如果 `g_words=0`，但 PA4 有 CS 边沿：
   - 重点查 **A13→PA5** 和 **A15→PA7** 两根，以及 GND。
   - 确认 P7 的 **pin 1/2/3** 没有接反。
   - 确认 FPGA GPIO0 VCCIO = 3.3V。
4. 串口 0 字符不影响音频；先保证 SPI 和 DAC。

## 5. 当前固件状态

> ⚠️ 2026-10-22 修正：原 `firmware.hex`（10-20 构建）含一处**致命 bug**——
> `gpio_init()` 里 `GPIOB_CRH` 写成 `0xB4344444u`，把 PB13(SPI2_SCK) 配成普通
> 推挽输出（停在低电平）、PB12(CS̄) 配成浮空输入（根本没被驱动），导致 MCP4922
> 永远收不到片选和时钟 → **完全无声**。已改为正确的 `0xB4B34444u`（PB13=0xB、
> PB12=0x3）并重新编译（见 `mcu/f103_spi_mcp4922/firmware.hex`，18:10 构建）。
> 另：`tool\烧录STM32.bat` 原先会优先烧 Design B（`spi2_dma_dac.hex`，STM32 内部
> DAC，与当前 MCP4922 接线不符），已改为默认烧 Design A（本目录固件）。

固件待烧录（**请重新烧录修正后的版本**）：

- 64 MHz + `FLASH_ACR=2 wait states`
- PRIMASK 临界区恢复逻辑修正
- EXTI4 NVIC 中断号改为 IRQ10
- UART1 115200、PA9 空闲高
- `GPIOB_CRH=0xB4B34444u`（PB13=SPI2_SCK 复用推挽 / PB12=CS̄ 推挽输出）— **2026-10-22 修复**

固件文件：

```
mcu/f103_spi_mcp4922/firmware.hex
mcu/f103_spi_mcp4922/firmware.elf
mcu/f103_spi_mcp4922/firmware.bin
```

烧录：双击 `tool\烧录STM32.bat`。
