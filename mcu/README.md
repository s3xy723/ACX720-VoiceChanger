# 单片机侧：把 FPGA 变声后的音频推到音响（真双声道）

这条文档讲一件事：**FPGA 处理完的双声道数字音频，怎么送到单片机，再由单片机
变成模拟立体声推动两个音箱。**

## 整体链路

```
  麦克风
    │  （模拟）
    ▼
 ES8388（板载 codec，ADC）           ← FPGA 通过 I2C 配置
    │  I2S 数字音频（BCLK / LRCK / ADCDAT）
    ▼
 FPGA（XC7A35T）变声处理
    │
    ├── aud_dacdat ──► ES8388（DAC）──► 板载耳机口      ← 原来就有的通路
    │
    └── rtl/i2s_mirror_tx.v ──► GPIO0 三条线 ──► STM32F103
                                                    │  I2S2 从机接收
                                                    ▼
                                              DAC1(PA4) / DAC2(PA5)
                                                    │  两路同时更新，零相差
                                                    ▼
                                              两级 RC 低通 + 隔直
                                                    ▼
                                              PAM8403 功放（3W × 2）
                                                    ▼
                                              左音箱 / 右音箱
```

关键点：**单片机不产生任何音频时钟**。BCLK / LRCK 由 ES8388 产生，FPGA 只是
把它们同步后复制一份送给单片机。所以单片机是 I2S 从机，两边永远同步，
不需要异步 FIFO、不需要采样率转换、也不会漂移。

## 一、接线

### 1.1 FPGA ↔ STM32（只有 4 根线，其中 1 根是地）

| ACX720 GPIO0 排针 | FPGA 引脚 | 信号名（Verilog） | STM32F103C8T6 | 说明 |
|---|---|---|---|---|
| GPIO0-0 | **A13** | `mcu_bclk`  | **PB13** | 位时钟 1.5625 MHz，I2S2_CK |
| GPIO0-1 | **A14** | `mcu_lrck`  | **PB12** | 左右声道选择 24.414 kHz，I2S2_WS |
| GPIO0-2 | **A15** | `mcu_dout`  | **PB15** | 串行数据，I2S2_SD |
| GND（排针上的地） | — | — | **GND** | **必须接**，不共地数据全是乱码 |

> GPIO0 的引脚号来自官方《ACX720 型 FPGA 开发板用户自助服务手册》
> （corecourse.cn tid=28730）里的 XDC 片段：`A13→GPIO0_bit9_0[0]`、
> `A14/A15` 同组；另一份官方《ACM1030 扩展模块管脚对应表》里也写着
> ACX720 的 `GPIO0-0 = A13`、`GPIO0-1 = A14`、`GPIO0-2 = A15`。两份一致。
> 排针上 VCC / GND 的位置以你板子的丝印和管脚表为准。

**注意三件事：**

1. **一定要共地。** 两边的 GND 不连，BCLK 对单片机来说就是浮空的，
   收到的数据全是随机的。这是最常见的"没声音"原因。
2. **不要从 FPGA 板给功放取电。** PAM8403 满功率要 1 A 以上，
   FPGA 板的 5 V 排针带不动，会拉垮整板。功放单独供电。
3. 线长尽量短（< 20 cm）。1.5625 MHz 不算高，但飞线太长会振铃，
   导致采样到错误的位。有条件用排线里相邻的地线夹住信号线。

### 1.2 STM32 → 滤波 → PAM8403

```
PA4 (DAC1 = 左) ──┬── 4.7k ──┬── 10k ──┬── 10µF ──► PAM8403  L 输入
                  │          │         │
                 2.2nF      1nF        │
                  │          │         │
                 GND        GND        │
                                       │
PA5 (DAC2 = 右) ──┬── 4.7k ──┬── 10k ──┴── 10µF ──► PAM8403  R 输入
                  │          │
                 2.2nF      1nF
                  │          │
                 GND        GND
```

| 元件 | 取值 | 作用 |
|---|---|---|
| 第一级 RC | 4.7 kΩ + 2.2 nF（fc ≈ 15.4 kHz） | 重建滤波，去掉 24.4 kHz 的采样镜像 |
| 第二级 RC | 10 kΩ + 1 nF（fc ≈ 15.9 kHz） | 二级滚降，镜像再衰 6 dB |
| 隔直电容 | 10 µF 电解（+ 端朝 STM32） | DAC 静音时输出 1.65 V 直流，必须隔断 |

**为什么 fc 取 15 kHz 而不是 20 kHz：** 本工程 ES8388 的采样率是
**24.414 kHz**（= MCLK 12.5 MHz / 512），奈奎斯特只有 12.2 kHz。
滤波器截止频率必须落在 12.2 kHz 和 24.4 kHz 之间，15 kHz 正好居中。

**为什么两级而不是一级：** 单级 RC 只有 6 dB/oct，从 15 kHz 到 24.4 kHz
才衰 4 dB，镜像会漏进功放。两级衰约 8~10 dB，配合 PAM8403 自己的输入滤波
足够了。要求高就换成二阶有源滤波。

**PAM8403 侧：**

| PAM8403 | 接哪里 |
|---|---|
| VCC | 外部 5 V（≥ 1 A），**不要**取自 FPGA 板 |
| GND | 与 STM32、FPGA 共地 |
| L / R 输入 | 上图两个 10 µF 的输出端 |
| 喇叭端子 L+/L−、R+/R− | 直接接 4~8 Ω 音箱（BTL 输出，两端都不接地） |

**开机顺序**：先把 `VOL_SHIFT` 设成 2（-12 dB）、功放电位器拧到最小，
确认有声后再慢慢加大。DAC 满幅是 1.65 V 峰值（约 1.17 V RMS），
配 PAM8403 的 24 dB 固定增益很容易过载失真。

## 二、STM32 侧的关键配置（这几处最容易配错）

| 项目 | 值 | 为什么 |
|---|---|---|
| I2S 外设 | **SPI2 / I2S2**（PB12/13/15） | F103C8 上 I2S2 就在这三个脚，不用重映射 |
| 主从 | **从机接收**（`I2SCFG = 01`） | 时钟由 FPGA 侧产生 |
| 标准 | **Philips**（`I2SSTD = 00`） | 与 `rtl/i2s_slave.v` 一致 |
| 数据/通道长度 | **DATLEN = 00，CHLEN = 1** | 16 bit 数据 + 32 bit 通道 = 标准库的 `I2S_DataFormat_16bextended`（值 0x0001） |
| CKPOL | 低 | BCLK 空闲为低 |
| SPI_CR1 | 只设 **SSM**（0x0200），SSI = 0 | ★ 见下方说明 |
| DAC | 通道 1（PA4）+ 通道 2（PA5），**不用触发**（TEN = 0） | 写 `DHR12RD` 一次更新两路 |
| DAC 缓冲 | **打开**（BOFF = 0） | 关掉输出阻抗太高，带不动后面的 RC 网络 |

### ★ 为什么 CR1 必须设 SSM

SPI2 的硬件 NSS 脚**恰好也是 PB12**，而 PB12 在 I2S2 模式下是 WS（LRCK）。
如果让硬件 NSS 生效，LRCK 为高的那一半帧（右声道）就会被判成"片选未选中"，
**右声道直接收不到**。设 SSM = 1 让 NSS 由内部 SSI 决定，SSI = 0 表示
从机始终被选中，接收不再受 PB12 电平影响。

### ★ 为什么 ISR 要用 CHSIDE 跳变门控

RM0008 对 16bit-extended 下 RXNE 的次数说得含糊：

> "Depending on the data and channel length configuration, the audio value
> received for a right or left channel may result from **one or two**
> receptions into the Rx buffer."

没给死。所以 ISR 不能假设"一次 RXNE = 一个声道"，改成：

```c
if (side != g_side) {        /* CHSIDE 变了 -> 这是有效数据半字 */
    g_side = side;
    ... 存样本 / 写 DAC
} else {
    g_dummy++;               /* 同一个声道下再来 -> 是补零段，丢掉 */
}
```

这样**无论硬件给 1 次还是 2 次 RXNE，解出来的样本都正确**。
`tool/verify_mcu_decode.py` 两种模式都测了，还带一个负对照：
不做门控的朴素 ISR 在 2 次模式下 8/8 帧全错。

串口每 500 ms 会打印 `dummy=`，实测一眼就知道你的芯片是哪种：
- `dummy=0` → 每声道 1 次 RXNE
- `dummy≈fs` → 每声道 2 次（门控正在帮你丢补零半字）

## 三、编译与下载

### Keil MDK

1. 新建工程，器件选 **STM32F103C8**（Medium Density，Flash 64K）。
2. 添加 `mcu/i2s_stereo_dac.c`，勾选 CMSIS → CORE 和 Device → Startup
   （启动文件 `startup_stm32f10x_md.s`，向量表由它提供，本 C 文件不再定义）。
3. Options → C/C++ → Define 里按需加 `VOL_SHIFT=2` 或 `SWAP_LR=1`。
4. 下载器选 ST-Link，Flash 地址 0x08000000。

### GCC（arm-none-eabi）

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -ffreestanding \
    -T stm32f103c8.ld -o out.elf i2s_stereo_dac.c startup_stm32f10x_md.c
arm-none-eabi-objcopy -O binary out.elf out.bin
```

## 四、验收清单（Pass / Fail）

| # | 项目 | 怎么验 | 通过标准 |
|---|---|---|---|
| 1 | FPGA 综合实现 | `vivado -mode batch -source tool/run_impl.tcl` | 0 Error / 0 Critical Warning，WNS ≥ 0 |
| 2 | 镜像引脚分配 | 看 `vivado_prj/io_report.rpt` | `mcu_bclk=A13`、`mcu_lrck=A14`、`mcu_dout=A15`，LVCMOS33 输出 |
| 3 | 镜像位流 == codec 位流 | `python tool/sim_i2s_mirror.py` | 640 位逐位相同 |
| 4 | Philips 时隙结构 | 同上 | 时隙 0 = 0，1..16 = bit15..bit0，17..31 = 0 |
| 5 | STM32 解码正确 | 同上（喂真实波形） | 逐样本与 FPGA 装载一致，左右严格交替 |
| 6 | 时序裕量 | 同上 | 数据翻转点离 BCLK 上升沿 ≥ 150 ns（实测 300 ns） |
| 7 | I2S 位定义 | `python tool/verify_mcu_decode.py` | I2SCFGR = 0x0901，低 3 位 = 0x0001 |
| 8 | ISR 门控两种模式都对 | 同上 | 1 次 / 2 次 RXNE 都解对；负对照必须失败 |
| 9 | DAC 量化与打包 | 同上 | 0x0000→2048、0x7FFF→4095、0x8000→0；CH1 = 左 |
| 10 | 上电有 I2S 时钟 | 串口 500 ms 一行 | `fs=24414 Hz` 左右（允许几十 Hz 偏差） |
| 11 | 有溢出吗 | 串口 `ovr=` | 恒为 0。非 0 说明 ISR 太慢或中断被长时间关闭 |
| 12 | 双声道独立 | 左耳（左音箱）只出左声道 | 上位机「分离」模块下男声只从左音箱出 |
| 13 | 变调生效 | 对麦克风说话，改「音高偏移」 | 两个音箱的音高一起变 |
| 14 | 无爆音 | 启动 / 停止 / 切模式 | 没有"啪"的一声 |

## 五、故障排查

| 现象 | 最可能的原因 | 怎么查 |
|---|---|---|
| 串口一直 `!! 一帧都没收到` | GND 没共地；或三根线接错/虚焊 | 万用表量 PB12/PB13 有没有 1.5 MHz 方波 |
| `fs=` 有数但完全无声 | DAC 隔直电容没接或方向反；功放没供电 | 示波器看 PA4 有没有台阶波 |
| 有声音但只有一边 | 一路 RC 滤波虚焊；或 10 µF 极性反 | 交换左右输入，看是不是跟着线走 |
| 声音"沙沙"、有金属味 | I2S 收到的是错位的位 | 看 `ovr` 是否非 0；检查飞线是否太长 |
| 左右反了 | — | 改 `SWAP_LR=1` 重新编译，或直接换音箱接线 |
| 声音很闷 | 采样率只有 24.414 kHz | 正常现象。想更亮要把 ES8388 改成 48 kHz（改 `rtl/es8388_init_table.v`） |
| 破音、过载 | DAC 满幅 + PAM8403 24 dB 增益 | `VOL_SHIFT` 改 2，功放电位器调小 |
| `dummy` 一直增长但没声音 | 门控逻辑没生效 | 确认 `I2S_CHLEN_32` 真的是 bit 0（见验收 #7） |

## 六、已知限制（诚实说明）

1. **采样率 24.414 kHz，不是 48 kHz。** 这是 ES8388 在 MCLK = 12.5 MHz 下
   的分频结果（/512）。人声够用，音乐的高频会明显偏闷。要改就得动
   `es8388_init_table.v` 里的分频配置，同时把本文件里 RC 滤波的 fc 跟着调。
2. **DAC 输出有中断抖动。** 从机模式下采样时刻由 FPGA 决定，但 DAC 的更新
   时刻取决于 ISR 响应速度，抖动约几百 ns 到 1 µs。24.4 kHz 下相当于
   -30 dB 左右的噪声底，人声听感上基本听不出来，但不是 Hi-Fi 做法。
   要做到无抖动得让 DAC 由定时器触发 + 用 DMA 做缓冲，代价是引入
   异步采样率转换（两边时钟不同源），反而更容易出问题，所以这里没做。
3. **12 bit DAC。** F103 的片内 DAC 只有 12 bit，比 FPGA 送出的 16 bit
   少 4 bit，动态范围约 72 dB。听人声够了。
4. **本机没有 ARM 编译器**，所以程序没有做过编译验证。替代方案是
   `tool/verify_mcu_decode.py`：把 ISR 逻辑逐行移植到 Python，
   喂 `sim_i2s_mirror.py` 产生的真实波形做验证，另外静态解析 C 源码里
   的每一个寄存器位定义并断言拼出来的值。语法层面只做了括号配平检查。

## 七、文件

| 文件 | 作用 |
|---|---|
| `mcu/i2s_stereo_dac.c` | STM32F103 程序（寄存器级，单文件，不依赖 HAL/SPL） |
| `rtl/i2s_mirror_tx.v` | FPGA 侧：把 `tx_left/tx_right` 从 GPIO0 再串行一份 |
| `constr/voice_changer.xdc` | A13/A14/A15 的引脚约束（文件末节） |
| `tool/sim_i2s_mirror.py` | 镜像总线的行为级仿真（12 项断言） |
| `tool/verify_mcu_decode.py` | 单片机位定义 / ISR 解码 / DAC 打包验证（26 项断言） |
