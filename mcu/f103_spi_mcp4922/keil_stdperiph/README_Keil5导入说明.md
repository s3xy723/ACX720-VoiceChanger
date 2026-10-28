# Keil5 导入说明 —— STM32F103C8T6 全 SPI 音频桥（标准库版 v1.1）

本目录只有 **两个源文件**（`main.c` + `stm32f10x_conf.h`），逻辑与寄存器级 v1.0 固件完全一致，
已用 arm-gcc 对照 **StdPeriph V3.5 真实头文件** 编译验证（零警告零错误）。

## 一、文件清单

| 文件 | 说明 |
|---|---|
| `main.c` | 全部业务逻辑：初始化 + 4 个中断服务 + 主循环状态机 |
| `stm32f10x_conf.h` | 标准库配置头（定义 `USE_STDPERIPH_DRIVER` 后必含） |

> 中断服务函数（`EXTI4_IRQHandler` / `SPI1_IRQHandler` / `TIM2_IRQHandler` / `SysTick_Handler`）
> 都定义在 `main.c` 里。**如果你的工程模板自带 `stm32f10x_it.c` 且里面有同名空函数，删掉 `it.c`
> 里的同名函数**（或整个注释掉），否则链接报 duplicate symbol。

## 二、快速导入（推荐：套用你已有的标准库模板）

1. 打开你现成的 STM32F103 **标准库**工程模板（含 `Libraries/CMSIS` + `STM32F10x_StdPeriph_Driver`）；
2. 把本目录两个文件复制进工程的 `User/`（或 `Src/`）目录；
3. 把 `main.c` 加进 User 组；确保 Include Paths 里有：
   - `CMSIS/CM3/CoreSupport`
   - `CMSIS/CM3/DeviceSupport/ST/STM32F10x`
   - `STM32F10x_StdPeriph_Driver/inc`
4. C/C++ → Preprocessor Symbols 里必须有：
   - `STM32F10X_MD`（中容量，C8T6）
   - `USE_STDPERIPH_DRIVER`
5. 删掉模板自带的 `main.c`（或替换），保证工程里只有一个 `main()`；
6. 驱动组里必须勾选/包含这些 `.c`（其余可不要）：
   ```
   stm32f10x_rcc.c   stm32f10x_gpio.c  stm32f10x_spi.c
   stm32f10x_exti.c  stm32f10x_tim.c   stm32f10x_usart.c
   stm32f10x_flash.c  misc.c
   ```
   以及 CMSIS 侧：`system_stm32f10x.c` + 启动文件 `startup_stm32f10x_md.s`
   （汇编版，注意选 **md** 不是 ld/md/cl）。

## 三、从零新建工程（没有模板时）

1. Project → New uVision Project，器件选 `STM32F103C8`；
2. 把 StdPeriph V3.5 整包的 `Libraries/` 拷进工程目录
   （没有整包就到 ST 官网下 `stsw-stm32054.zip`，或找 V3.5 镜像）；
3. 管理组：`CMSIS`（system + startup）、`Drivers`（上面 8 个 .c）、`User`（main.c）；
4. 按第二节第 3、4 条配置 include 与宏；
5. Debug 选项卡选你的下载器（ST-Link Debugger / CMSIS-DAP），
   Flash Download 勾 `Reset and Run`，烧完自动跑。

## 四、引脚与行为（与寄存器版一致）

| STM32 引脚 | 方向 | 对端 |
|---|---|---|
| PA5 / PA4 / PA7 | 输入 | FPGA A13/SCLK、A14/CS̄、A15/MOSI（SPI1 从机，≈2.08 MHz） |
| PB13 / PB15 / PB12 | 输出 | MCP4922 SCK / SDI / CS̄（SPI2 主机，4 MHz） |
| PB0 | 输出 | MCP4922 LDAC̄ |
| PA9 | 输出 | USB-TTL RX，115200-8-N-1 状态口 |
| PC13 | 输出 | 板载 LED（低有效）状态指示 |

行为不变：上电写静音中值（DAC 输出 1.65 V）→ 1 s 内无 FPGA 数据自动切 **440 Hz 自检音**
（TIM2 @24.42 kHz，验证 DAC→功放→喇叭）；FPGA 数据一来切直通；串口每 500 ms 打
`mode=... words/s=... fs=... ovr=...`，fs≈24414 且 ovr=0 即链路健康。

**★ LED 判据（没有 USB-TTL 时的验收方式）：**

| LED 现象 | 含义 |
|---|---|
| 上电先亮一下 | 程序已跑起来（初始化完成） |
| 慢闪（1 Hz） | TEST 模式：没收到 FPGA 数据 → 检查 FPGA 位流是否下载 / 三根线 + 共地 |
| **常亮** | PASS 模式：FPGA 数据正在流入 → **SPI 链路打通，本次测试通过** |

## 五、验收清单（编译 + 烧录后）

- [ ] Keil 编译 0 Error（警告允许 "last line of file ends with newline" 一类）
- [ ] 烧录后**不接 FPGA**：喇叭出 440 Hz 稳定音
- [ ] 串口见 `mode=TEST(440Hz) words/s=48828 fs=24414`
- [ ] 接好 FPGA 三线 + 共地：声音变为直通音频，串口 `mode=PASS`
- [ ] `ovr` 长时间保持 0

## 六、常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 链接报 duplicate symbol（EXTI4 等） | 模板 `stm32f10x_it.c` 里有同名空中断 | 删掉 it.c 里的同名函数 |
| 报 `USE_STDPERIPH_DRIVER` 未定义 | 宏没加 | 按第二节第 4 条补两个宏 |
| `misc.h not found` | include 路径缺 StdPeriph_Driver/inc | 补路径 |
| `cannot open source file core_cm3.h` | CMSIS 路径没加 | 补 CoreSupport 路径 |
| 烧完没声音 | PB0/PB12 未接、或 SHDN̄(10脚) 悬空 | 查线；SHDN̄ 必须接 3.3V |
| 自检音有但不通 FPGA | SPI1 三线接错或没共地 | 示波器看 PA5 应有 2 MHz 时钟 |
