# ACX720-V3 (XC7A35T) 实时变声器

在 Xilinx Artix-7 XC7A35T 上跑的实时语音效果器。单 50 MHz 时钟域、不用 PLL、
不用 IP 核，纯 RTL + 手写 I2C 主机 + I2S 从机。

四种效果，用板上的 8 位拨码开关现场切换：

| sw[7:6] | 效果 | 说明 |
|---------|------|------|
| `00` | 直通 | **上电后第一步必须先用这个**，用来判断硬件通路通不通 |
| `01` | 环形调制 | 机器人 / 达斯·维达声，`sw[2:0]` 选调制频率 |
| `10` | 变调 | 核心功能。双抽头 Hann 延迟线，**变调不变时长** |
| `11` | 回声 | 前馈梳状滤波器，`sw[3:0]` 调延迟，`sw[5:4]` 调反馈量 |

> **想先听效果又暂时上不了板？** 看 [`docs/README_HOST.md`](docs/README_HOST.md)。
> `host/` 里是一套同样算法的 PC 上位机（麦克风实时变声 + 文件变声），
> 不需要连开发板。最省事的是直接用打包好的
> **`dist/ACX720VoiceChanger/ACX720VoiceChanger.exe`**
> （文件夹版，免装 Python；不要用单文件版，单文件在这台机器上会报
> "Could not create temporary directory!"）；桌面快捷方式
> `ACX720变声器.lnk` 已指向它。也可以双击 `host/运行上位机.bat` 走源码模式。
> 界面带 **四路波形（输入 L/R + 输出 L/R）** / 频谱对照两种视图和 15 盏状态校验灯。
> 文件模式**吃 WAV / FLAC / MP3 / M4A / OGG / OPUS / AIFF…**，
> 分离模式固定 **左声道=人声、右声道=伴奏**，可另导出两条音轨；
> 文件页内嵌播放器，支持进度/音量与**左右声道独立开关**；播放器一行最右侧还能
> **单独选输出设备** —— 选 `CABLE Input (VB-Audio Virtual Cable)` 就把试听声音送进
> FPGA→STM32→音响那条链路，选「（系统默认）」则走本机扬声器。
> 设备名与完整链路见 [`docs/音频输出路由_2026-10-21.md`](docs/音频输出路由_2026-10-21.md)。
> 它是本工程 RTL 的逐比特移植，界面右下角还会实时显示当前参数
> 对应板上的哪个拨码值，调好了直接搬过去。

---

## 0. 目录结构（2026-10-21 整理）

按「源码 / 脚本 / 文档 / 资源 / 产物」分层。**构建与运行只依赖前四类**，
`dist/` 与 `vivado_prj/` 都是可以整目录删掉重新生成的。

仓库根目录**就是工程根目录**（2026-10-28 整理时把原来的 `acx720_voice_changer/`
上提了一层）。所有脚本都用「相对自身位置」推导路径，因此克隆到任何位置都能跑；
唯一例外是 Vivado 工程文件 `vivado_prj/` 里的绝对路径，那部分由 `.gitignore` 排除，
用 `tool/build_uart_audio.tcl` 重新生成即可。

| 目录 | 内容 | 备注 |
|------|------|------|
| `host/` | 上位机 Python 源码、`build_exe.bat`、两个 `.spec`、`icon.ico` | 打包入口全在这；**不要拆散**（spec 用相对路径找 `host_app.py` / `icon.ico`） |
| `rtl/` | FPGA Verilog 源码（顶层 `voice_changer_top.v`） | `_legacy/` 是第一版弃用实现 |
| `mcu/` | STM32F103 固件源码（音响驱动 `spi2_dma_dac.c`、启动文件、链接脚本） | 见 `mcu/README.md` |
| `constr/` | Vivado 引脚 / 时钟约束 `.xdc` | |
| `sim_src/` | 仿真 testbench 与激励数据 | |
| `tool/` | 脚本工具箱（Python 分析/校验、Vivado Tcl、烧录 BAT） | 保持扁平：多个脚本互相 `import`，分子目录会断链 |
| `tool/thirdparty/` | `openocd/`、`stlink_driver/`、`vbcable/` 第三方二进制 | 烧录脚本用 `%~dp0thirdparty\…` 引用 |
| `tool/logs/` | FPGA 烧录 / 识别日志 | **已移出仓库**（`_cleanup_*/`），排障用、不是构建输入 |
| `tool/legacy/` | 3 个硬编码旧机器路径的 Tcl | **已移出仓库**，在本机跑不通，仅留档 |
| `docs/` | 全部 Markdown 文档（布置说明、验收报告、分离算法精校、接线表…） | `docs/images/` 放插图 |
| `samples/` | 三个年龄变声试听件（16 bit WAV） | 其余对比素材（`ab_compare*/`、`demo/`，约 75 MB）已移出仓库 |
| `vivado_prj/` | Vivado 工程、`.bit` 比特流、时序 / 资源报告 | 可由 `tool/run_impl.tcl` 重新生成 |
| `dist/` | **最终产物**（文件夹版 exe） | 见下；`.gitignore` 已排除 —— **克隆后没有这个目录**，按 `host/build_exe.bat` 自己打一份即可 |
| `LICENSE` | MIT | |
| `.gitignore` | 构建产物 / 运行日志 / 大文件的忽略规则 | |
| `_cleanup_20261028/` | 本次整理移出的旧包、构建中间目录、评测音频 | 确认无误后可整目录删除 |

构建中间产物（`build_onedir/`、`build_pw/`、`work/`、`__pycache__/`、`xsim.dir/`）
已清理，需要时会自动重建。

### 0.1 打包结果（2026-10-21）

| 项 | 值 |
|----|----|
| 产物 | `dist/ACX720VoiceChanger/ACX720VoiceChanger.exe` |
| 形式 | onedir 文件夹版（**不是**单文件，避免 `%TEMP%` 解压失败） |
| 体积 | exe 6.95 MB + `_internal/` 共 **143.1 MB / 1711 个文件** |
| 解释器 | CPython **3.12.14**（`.dsh` 运行时；自带 PyInstaller 6.22.3） |
| 打包命令 | `python -m PyInstaller --noconfirm --distpath ..\dist --workpath ..\build_onedir ACX720VoiceChanger.spec` |
| 自检 | `ACX720VoiceChanger.exe --selftest` → **27 项全 PASS**（结果写当前目录 `_selftest.txt`） |
| 源码测试 | `python host/test_host.py` → **126 PASS / 0 FAIL** |

打包要点（踩过的坑，改之前先读）：

1. **必须吃 `.spec`，不要走命令行 `--exclude-module`**。后者管得住模块图，
   管不住 hook 收集来的 DLL，会打出一个 2.2 GB 且 `import torch` 就崩的废物。
2. 轻量版靠 spec 里的 `DROP-TORCH` 过滤器在 `Analysis` 之后剔除
   torch / onnxruntime / nvidia 的 binaries、datas、pure，体积 2.2 GB → 143 MB。
3. 用 onedir 而不是 onefile：单文件版每次启动要往 `%TEMP%\_MEIxxxx` 解压，
   某些机器（杀软 / 临时目录策略）直接弹 `Could not create temporary directory!`。
4. 想连 AI 完整版一起打：`build_exe.bat ai`（`dist_ai/`，约 4.8 GB，自带 torch）。

---

## 1. 硬件：板载 codec 是 ES8388（不是 WM8731）

这一个结论改掉了整套 I2S 架构，来源是两份互相印证的官方资料：

1. `ACX720-V3管脚分配表2024_11_13.xlsx`（04_引脚功能信息）
2. 官方例程 `02 设计实例/ACX720_V3_35T/ch35_CoreCourse_GHRD_I2S_RX.rar`
   里的 `constrs_1/new/IO.xdc`（这个例程自带可下载的 `.bit`，论证过）

| 信号 | 管脚 | 方向 | 说明 |
|------|------|------|------|
| `clk` | Y18 | 入 | 50 MHz 系统时钟 |
| `reset_n` | B21 | 入 | S4 复位按键（低有效） |
| `i2c_sclk` | M13 | 出 | ES8388 配置口，器件地址 8'h20（7 位 0x10） |
| `i2c_sdat` | M16 | 双向 | 同上 |
| `aud_mclk` | V20 | **出** | XCLK，FPGA 送给 codec，本工程 12.5 MHz |
| `aud_bclk` | U20 | **入** | codec 主模式，BCLK 由 codec 给 FPGA |
| `aud_lrclk` | W20 | **入** | 同上 |
| `aud_adcdat` | AA21 | 入 | codec → FPGA |
| `aud_dacdat` | AA20 | 出 | FPGA → codec |

**关键点：ES8388 跑在主模式**，BCLK / LRCLK 是 codec 送给 FPGA 的，所以本工程
的 I2S 是从机（`rtl/i2s_slave.v`），不是之前那版自己产生时钟的主机。
官方例程用异步 FIFO 跨时钟域，本工程改成 **50 MHz 过采样**（BCLK 只有 1.5625 MHz，
32 倍过采样），省掉了 FIFO 和一块 BRAM。

---

## 2. 时钟与采样率

| 信号 | 来源 | 频率 |
|------|------|------|
| MCLK | 50 MHz 四分频（顶层 `mclk_r`） | 12.500 MHz |
| BCLK | codec 内部分频 MCLK/8 | 1.5625 MHz |
| LRCK | codec 内部分频 MCLK/512 | 24.414 kHz（= 采样率） |
| BCLK/LRCK | — | 64，即每声道 32 个 BCLK 时隙、16 bit 有效 |

为什么是 12.5 MHz 而不是标准的 12.288 MHz：板上只有 50 MHz 晶振，而 50 MHz
经 MMCM 出不来 12.288 MHz（需要 M/D 的小数分母含 5⁵，超出 7 系列 M 的上限）。
codec 内部只做整数分频，对 MCLK 绝对精度不敏感，所以直接用 12.5 MHz，
代价是采样率变成 24.414 kHz 而不是 24/48 kHz —— 语音完全够用。

想回到 16 kHz（官方例程默认值）只要把 MCLK 换成 8.192 MHz，**寄存器配置表不用动**。

---

## 3. 源码目录详解（逐文件）

```
acx720_voice_changer/
├── rtl/
│   ├── voice_changer_top.v    顶层
│   ├── es8388_init.v          上电延时 + 启动配置（官方例程原文件）
│   ├── es8388_init_table.v    43 个寄存器的配置表（官方例程原文件）
│   ├── i2c_init_dev.v         按表逐个写寄存器（官方例程原文件）
│   ├── i2c_control.v          I2C 协议层（官方例程原文件）
│   ├── i2c_bit_shift.v        I2C 位层（官方例程原文件）
│   ├── i2s_slave.v            ★ I2S 从机，50 MHz 过采样，收发各 16 bit
│   ├── i2s_mirror_tx.v        ★ 给单片机的 I2S 镜像输出（GPIO0 三根线）
│   ├── pitch_shift.v          变调核心：双抽头 Hann 交叉淡化延迟线
│   ├── effect_aux.v           环形调制 + 回声
│   ├── voice_proc.v           单声道效果链，实例化两次给左右声道
│   ├── stereo_meter.v         ★ 左右独立电平/削波 + 立体声相关性统计（驱动 LED）
│   ├── wave_rom.v             Hann 窗 + 正弦表（脚本生成，勿手改）
│   └── _legacy/               第一版（WM8731 + I2S 主机），已弃用
├── host/                      ★ PC 上位机（不需要板子就能用）
│   ├── dsp_core.py            效果链内核 + EngineChain（模块 × 引擎统一入口）
│   ├── hq_stream.py           流式相位声码器变调（FPGA 内部小模型的实时版）
│   ├── local_stream.py        流式 demucs 分离（计算机本地模型的实时版）
│   ├── vocal_split.py         ★ 分离内核（纯 DSP，左=人声 / 右=伴奏）
│   ├── hq_voice.py            离线高质量变调（文件模式质量最好的那条）
│   ├── local_ai.py            本地 AI 链路（demucs + 人声处理 + 重混）
│   ├── audio_io.py            文件读写 + 声卡设备枚举（★ WAV/FLAC/MP3/M4A/OGG/OPUS…）
│   ├── host_app.py            tkinter 界面：模块 × 引擎 × 输入 + 频谱 + 状态灯
│   ├── theme.py               ★ 深色主题（配色 / 字体 / ttk 样式集中在一处）
│   ├── spectrum.py            频谱绘制（★ 左右声道同时画，上=输入 / 下=输出）
│   ├── status_panel.py        16 盏状态校验灯板（削波按 L/R 分开 + 缓存）
│   ├── test_host.py           验收脚本（含真麦克风、人声/伴奏分离、格式兼容）
│   ├── build_exe.bat          打包成文件夹版（onedir）
├── mcu/                      ★ 外接单片机推音响（真双声道模拟输出）
│   ├── i2s_stereo_dac.c       STM32F103 程序：I2S2 从机收 → 双通道 DAC 出
│   └── README.md              接线表 / 滤波参数 / 验收清单 / 故障排查
├── dist/
│   └── ACX720VoiceChanger/    ★ 文件夹版上位机，双击里面的 exe（免装 Python）
├── constr/
│   └── voice_changer.xdc      引脚约束（已全部填好）
├── sim_src/                   老版 TB（针对已弃用的 I2S 主机）
└── tool/
    ├── run_impl.tcl           综合 → 实现 → 生成 bit（一步到位）
    ├── program.tcl            下载 bit 到板（Digilent JTAG-HS2）
    ├── run_synth.tcl          只做综合
    ├── sim_i2s_slave.py       ★ I2S 时序的行为级仿真（纯 Python，PASS）
    ├── sim_i2s_mirror.py      ★ 单片机镜像总线的行为级仿真（12 项 PASS）
    ├── verify_mcu_decode.py   ★ 单片机位定义 / ISR 解码 / DAC 打包（20 项 PASS）
    ├── verify_theme.py        深色主题的程序化视觉校验（不依赖截图）
    ├── verify_engine_chain.py 上位机「模块 × 引擎 × 输入」结构验收（26 项 PASS）
    ├── sim_stereo_meter.py    ★ stereo_meter.v 的行为级仿真（纯 Python，16 项 PASS）
    ├── import_official_es8388.py  把官方例程的 ES8388 文件搬进来并转 UTF-8
    ├── gen_wave_rom.py        生成 wave_rom.v
    ├── ref_model_pitch.py     变调算法的逐比特参考模型
    ├── check_dphase_wiring.py 验证 dphase 端口符号性 + 全 16 档误差
    └── analyze_sim.py         分析仿真输出，做自相关基频估计
```

---

## 4. 变调算法

不需要 FFT。长度 512 的 Hann 窗，两个抽头相距半个窗长，每个抽头的权重就是
窗函数在该抽头延迟处的取值。

由于 `w(d) + w(d + 半窗) ≡ 1`：

- 输出速率恒等于 fs → **播放时长不变**
- 回绕时两抽头角色互换 → 不会出现咔哒声
- 两个抽头的延迟同时以固定速率漂移 → 音高改变

变调比 `r = 1 + dphase/64`。

### 拨码开关对照表

`sw[5:2]` = v，`dphase = (v - 5) × 6`，`r = 1 + dphase/64`

| v | r | 听感 |
|---|---|------|
| 0 | 0.53 | 最低男声（再低就跑调了，见第 7 节） |
| 2 | 0.72 | 男声化 |
| **5** | **1.00** | **原声** |
| 8 | 1.28 | 女声化 |
| 11 | 1.56 | 升五度 |
| 15 | 1.94 | 接近升八度，花栗鼠 |

每档间隔 `6/64 = 0.094`，约 1.6 个半音。

**`sw[7:6]=10` 时 `sw[5:2]` 才生效。**
例：原声 v=5 → `sw = 10_0101_00 = 0x94`；花栗鼠 v=15 → `sw = 10_1111_00 = 0xBC`

环形调制频率 `sw[2:0]`（`sw[7:6]=01` 时生效）：`60 / 100 / 150 / 220 / 350 / 500 / 800 / 1200 Hz`
回声延迟 `sw[3:0]`：24.414 kHz 下每档 256 点 ≈ 10.5 ms

---

## 5. 怎么用

### 一步生成比特流

```bash
cd acx720_voice_changer
vivado -mode batch -source tool/run_impl.tcl
```

产物：`vivado_prj/voice_changer.bit`，外加 `util_impl.rpt`、`timing.rpt`、`io_report.rpt`。

### 下载到板

```bash
vivado -mode batch -source tool/program.tcl
```

会自动找 `xc7a35t` 器件并烧写（Digilent JTAG-HS2）。
脚本里已经规避了 Vivado 2018.3 的几个坑：`open_hw`（不是 `open_hw_manager`）、
不设 `PARAM.FREQ`、`refresh_hw_device` 不带 `-update_hw_programs`。

### 不上 Vivado 的快速验证

```bash
python tool/sim_i2s_slave.py        # I2S 收发位序，4 帧全 PASS
python tool/sim_stereo_meter.py     # 左右电平表/削波/相关性，16 项全 PASS
python tool/ref_model_pitch.py      # 变调算法逐比特参考模型
python tool/check_dphase_wiring.py  # dphase 端口符号性 + 16 档误差
```

上位机那边（不需要板子，也不需要 Vivado）：

```bash
python host/test_host.py            # 验收：人声/伴奏分离、双声道独立性、格式兼容、长时间运行
python host/host_app.py --selftest  # 9 项自检，结果写 _selftest.txt
```

---

## 6. 已验证的结果

### 资源与时序（Vivado 2018.3，`xc7a35tfgg484-2`，**实现后实测**）

| 项 | 用量 | 占 xc7a35t |
|---|---|---|
| Slice LUTs | 1231 | 5.92% |
| Slice Registers | 833 | 2.00% |
| Block RAM Tile | 9 | 18.00% |
| DSP48E1 | 12 | 13.33% |
| **WNS** | **+3.618 ns** | 周期 20 ns，余量充足 |
| WHS | +0.059 ns | — |

`xvlog` 全工程 0 error / 0 warning；实现 0 error、0 critical warning
（89 条 warning 全是 `REQP-1840`：BRAM 地址寄存器带异步复位，属于建议项，见第 7 节）。

> 加了 `stereo_meter.v`（左右独立电平/削波 + 相关性统计）之后的对比：
> LUT 989 → **1231**、FF 756 → **833**、BRAM 9（不变）、DSP 12（**不变**，
> 新模块只用加法/移位/比较，一个乘法都没有）。
> 纯 Python 行为级仿真 16 项全 PASS：`python tool/sim_stereo_meter.py`。

### I2S 位序（`tool/sim_i2s_slave.py`）

RX：4 帧左/右声道解出的 16 位样本与 codec 发送的完全一致。
TX：codec 在 32 个 BCLK 上升沿采到的位序列 = `0` + `bit15..bit0` + `15×0`，符合 I2S 标准。

> 这个仿真一开始是 FAIL 的，它抓出了两个真实缺陷：
> ① 同步器的索引顺序写反（`{s[1:0], in}` 意味着 `[0]` 才是最新值，边沿检测要用 `[2:1]`）；
> ② `lrck_lvl` 取了延迟 2 拍的旧电平，导致左右声道装反，必须取延迟 1 拍的值。

### 变调精度（`tool/check_dphase_wiring.py`，纯音 440 Hz，全 16 档）

| v | 等效步进 | 理论输出 | 实测输出 | 误差 |
|---|---|---|---|---|
| 0 (r=0.53) | -30 | 233.8 Hz | 238.4 Hz | 1.99% |
| 4 (r=0.91) | -6 | 398.8 Hz | 399.7 Hz | 0.23% |
| **5 (r=1.00)** | 0 | 440.0 Hz | 440.0 Hz | 0.01% |
| 8 (r=1.28) | 18 | 563.8 Hz | 561.1 Hz | 0.47% |
| 11 (r=1.56) | 36 | 687.5 Hz | 682.2 Hz | 0.77% |
| 15 (r=1.94) | 60 | 852.5 Hz | 843.6 Hz | 1.04% |

全 16 档最大误差 **1.99%**，判据 < 3% → PASS。

---

## 7. 已知局限

1. **持续单音会有约 25% 的包络起伏**（约 2.5 dB）。
   这是时域变调方案的固有属性，不是 bug：两个抽头固定相差半个窗长，
   当输入频率的周期不能整除这个间隔时，交叉淡化中点处两路会部分相消。
   语音是宽带信号，各频率成分相互错开，实际听感是轻微的金属感 / shimmer。

2. **左右声道各一条独立效果链，但共用同一套参数**（拨码只有一组）。
   延迟线、相位、回声历史是**分开**的，绝不做 L/R 混音；只是"调什么"两边一样。
   上位机里多一个 **分离**模式（左=人声 / 右=伴奏），
   板子上没有对应实现——见 [`docs/README_HOST.md`](docs/README_HOST.md)。

3. **没有自动增益控制**。输入音量过大时会削顶，调 ES8388 的输入增益
   （寄存器 0x09 / 0x0A 相关位）或在顶层加电平控制。

4. **fs = 24.414 kHz**，带宽约 12 kHz。语音足够，音乐会有明显的高频缺失。
   要 48 kHz 需要换一个能整除出 12.288 MHz 的主时钟。

5. **变调比下限是 r ≈ 0.53，不能再低**：

   | r | 0.125 | 0.25 | 0.375 | 0.5 | 0.625 | 0.75 |
   |---|---|---|---|---|---|---|
   | 频率误差 | 17% | 7% | 3.8% | 2.3% | 1.3% | 0.75% |

   所以拨码映射取 `(v-5)×6`（r ∈ [0.53, 1.94]），而不是能到 r=0.125 的 `(v-7)×8`。

6. **`dphase` 端口必须声明成 `signed`**：从顶层（9 位有符号）传到
   `pitch_shift` 时如果端口是无符号就会零扩展，`-32` 的补码 `1_1110_0000`
   会变成 `480`，相位反着跑 —— 所有降调档位都会变成刺耳的升调。
   （这是本项目修掉的最隐蔽的一个 bug。）

7. **89 条 `REQP-1840` warning**：`pitch_shift` 里驱动 BRAM 地址的相位寄存器
   带异步复位，Vivado 提示复位释放瞬间 BRAM 读出值可能不对。
   实际影响只是复位刚释放时可能有一声轻微爆音，不影响正常工作；
   要彻底消除就得把 `phase` 改成同步复位。

8. **整条链路固定 1 帧（41 μs）延迟**，来自 I2S 收发各锁存一帧。人耳无感。

---

## 8. 上板后的调试顺序

1. `sw = 0x00`（直通），对麦克风说话 / 从 LINE IN 灌音源，耳机插 LOUT。
   **这一步必须出声**，出不来就是引脚或 codec 配置的问题，跟 DSP 无关。
2. 看 LED（8 颗全部有确定含义）：

   | LED | 含义 |
   |---|---|
   | `led[1:0]` | 当前效果模式（= `sw[7:6]`） |
   | `led[2]` | I2C 配置跑完（43 个 ES8388 寄存器写完） |
   | `led[3]` | **左**声道有信号 |
   | `led[4]` | **右**声道有信号 |
   | `led[5]` | **左**声道削波（闪一下=偶发，常亮=持续过载） |
   | `led[6]` | **右**声道削波 |
   | `led[7]` | 左右确实不同（立体声相关性统计，单声道源这个灯一定灭） |

   说话时 `led[3]`/`led[4]` 会闪；只有一边闪就说明有一路没接上。
   `led[7]` 常灭但声音正常 = 两声道拿到的是同一个信号（音源是单声道，
   或 I2S 只解出了一路），这也是这套 LED 想让你一眼看出来的东西。
3. 确认通了之后再拨 `sw[7:6] = 01` 试环形调制 —— 这个辨识度最高。
4. 最后玩变调，`sw[5:2]` 从 5（原声）往两边试。

如果第 1 步没声音，按这个顺序排查：

- `aud_mclk`（V20）有没有 12.5 MHz（示波器 / 逻辑分析仪）
- `led[2]` 灭 = I2C 没跑完：查 SDA/SCL 上拉（xdc 里已开 PULLUP）、ES8388 地址
- `led[2]` 亮但没声：用逻辑分析仪看 `aud_bclk`（U20）有没有 1.5625 MHz。
  没有 = codec 没被配成主模式，查寄存器 0x08 的写入
- 有 BCLK 还是没声：确认输入通道。默认配置是 **LIN1/RIN1（Line IN）**；
  如果你的板子把麦克风接在 LIN2/RIN2，改 `rtl/es8388_init_table.v` 里的
  `rom[12] = 16'h0A_52;` 和 `rom[13] = 16'h0B_82;`，重新跑 `run_impl.tcl`
- 输入/输出插孔有没有插对（蓝色一般是输入，绿色是输出）

---

## 9. 外接单片机推音响（真双声道）

板载 ES8388 已经能出声了，但如果你要**推无源音箱**，就需要在 FPGA 后面
接一个单片机做 D/A 和功放驱动。工程里这条链路已经打通：

```
ES8388(ADC) → FPGA 变声 → GPIO0 三根线 → STM32F103(I2S2 从机)
             → 双通道 DAC(PA4/PA5) → RC 滤波 → PAM8403 → 左右音箱
```

只需要 **4 根线**（BCLK / LRCK / DATA / GND）：

| ACX720 GPIO0 | FPGA 引脚 | 信号 | STM32F103C8T6 |
|---|---|---|---|
| GPIO0-0 | A13 | `mcu_bclk` | PB13 |
| GPIO0-1 | A14 | `mcu_lrck` | PB12 |
| GPIO0-2 | A15 | `mcu_dout` | PB15 |
| GND | — | — | **GND（必须共地）** |

三个设计上的关键点：

1. **叫「镜像」不是「重发」**：`rtl/i2s_mirror_tx.v` 不自己造时钟，
   它把 codec 的 BCLK / LRCK 同步后寄存拷贝输出，数据位序列与
   `aud_dacdat` **逐位相同**（仿真验证 640 位全同）。整条链路零时钟域
   穿越、零速率匹配，也不会和 codec 那一路的 LRCK 错相位。
2. **单片机是从机**：采样率 24.414 kHz 由 ES8388 决定，单片机只跟着走，
   两边不可能漂移，因此不需要异步 FIFO。
3. **真双声道**：STM32 用 `DHR12RD` 一次写 32 位同时更新两个 DAC 通道，
   左右之间相位差为零；ISR 用 CHSIDE 跳变门控，兼容"每声道 1 次还是
   2 次 RXNE"的手册歧义。

完整接线表、RC 滤波参数（两级 15 kHz）、PAM8403 接法、Keil/GCC 编译步骤、
14 项 Pass/Fail 验收清单与故障排查表见 **`mcu/README.md`**。

相关验证脚本：

```
python tool/sim_i2s_mirror.py      # 镜像总线 12 项断言（位流/时隙/解码/裕量）
python tool/verify_mcu_decode.py   # 单片机 26 项断言（位定义/ISR/DAC）
```
