`timescale 1ns / 1ps
/* ============================================================================
 *  spi_mirror_tx.v —— 给 STM32 的 SPI 镜像总线（全 SPI 方案，替代 i2s_mirror_tx）
 *
 *  电气约定（与 STM32 固件 mcu/spi2_dma_dac.c 严格对齐）：
 *    - SPI 主机，模式 0,0（CPOL=0 / CPHA=0，上升沿采样，MSB 先行）
 *    - 16 bit 一个字，一个帧里先左后右两个字
 *    - 位时钟 = 50 MHz / 24 / 2 ≈ 2.083 MHz（16 bit 占 384 周期 = 7.68 µs）
 *    - 帧周期 = 2048 个 50 MHz 时钟 = 40.96 µs  → fs = 24414.0625 Hz（不变）
 *
 *  ★ 2026-10-20 改：CS̄ 从"每字一个脉冲"改成"**每帧一个脉冲**"。
 *    旧版每个字都给一个 CS̄ 下降沿，而左右是靠本模块内部的 word_sel 计数
 *    器交替的 —— 单片机**看不到**这个计数器。后果是：只要丢一个字（上电
 *    时序、DMA 没来得及、OVR），左右就永久互换，表现为声道随机反、声音
 *    时好时坏，而且复位也未必能恢复。这就是"音响驱动不好用"的根因。
 *    现在 CS̄ 下降沿 = 帧起点 = 左声道字的起点，单片机用 EXTI 抓这个边沿
 *    就能硬同步，任何丢字都只影响当前一帧，不会累积成左右颠倒。
 *
 *  STM32 侧接线（ACX720 GPIO0 扩展口 → Blue Pill）：
 *    mcu_sclk (A13) -> PB13 (SPI2_SCK)
 *    mcu_csn  (A14) -> PB12 (EXTI12 帧同步；SPI2 用软件 NSS 常选)
 *    mcu_mosi (A15) -> PB15 (SPI2_MOSI)
 *    GND            -> GND（必须共地）
 *    内部 DAC 输出：PA4 = 左（DAC1）、PA5 = 右（DAC2）
 *    —— 换成 SPI2 就是为了让 PA4/PA5 空出来给 DAC，两者不打架。
 *
 *  取舍说明：时钟由本模块自产（不像 I2S 版照抄 codec BCLK/LRCK），所以与
 *  处理链的采样时钟同频不同相——样本偶发重复/跳过一个，听觉无感，
 *  换来 STM32 端不需要 FIFO、不需要任何时钟恢复。
 * ==========================================================================*/
module spi_mirror_tx (
    input  wire        clk,        // 50 MHz 系统时钟
    input  wire        rst_n,      // 低有效复位

    // ---- 处理后的样本（与 i2s_slave 的 tx_left / tx_right 同源）----
    input  wire [15:0] tx_left,
    input  wire [15:0] tx_right,

    // ---- 给单片机的 SPI 总线 ----
    output reg         mcu_sclk,   // ≈2.083 MHz，空闲为低（CPOL=0）
    output reg         mcu_csn,    // 每字一个低脉冲（下降沿 = EXTI4 同步）
    output reg         mcu_mosi    // MSB 先行
);

    // ------------------------------------------------------------------
    //  时序参数（50 MHz 主时钟）
    // ------------------------------------------------------------------
    // ---- 帧时序：一帧 = 左字 + 右字，总 2048 周期 = fs 24.414 kHz ----
    //   32 周期 CS̄ 高（让单片机 EXTI 抓到干净的下降沿）
    //   之后两个字各占 1008 周期（发送 384 + 空闲 624）
    localparam CNT_FRAME  = 2048;  // 帧周期（50 MHz / 2048 = 24414.0625 Hz）
    localparam CNT_CS_HI  = 32;    // 帧起始处 CS̄ 保持高的周期数
    localparam CNT_SLOT   = (CNT_FRAME - CNT_CS_HI) / 2;   // 1008，一个字的槽位
    localparam CNT_ACTIVE = 384;   // 16 bit × 24 周期/位（发送窗口）
    localparam PHASE_BIT  = 24;    // 每 bit 周期（前 12 低 = 数据建立，后 12 高）

    reg [10:0] cnt;                // 0..2047 帧内周期计数
    reg [4:0]  phase;              // 0..23 位内相位计数
    reg [3:0]  bitidx;             // 0..15
    reg [15:0] word_r;             // 本字要发的 16 bit（发送期间保持不变）

    // 两个"字槽"：槽 0 = 左声道，槽 1 = 右声道。全部用比较器切分，
    // 不用取模——取模在 FPGA 上是真除法器，白吃资源还可能时序不过。
    wire in_slot0 = (cnt >= CNT_CS_HI) &&
                    (cnt < CNT_CS_HI + CNT_SLOT);
    wire in_slot1 = (cnt >= CNT_CS_HI + CNT_SLOT) &&
                    (cnt < CNT_FRAME);
    wire [10:0] ofs0 = cnt - CNT_CS_HI;                    // 槽 0 内偏移
    wire [10:0] ofs1 = cnt - (CNT_CS_HI + CNT_SLOT);       // 槽 1 内偏移
    wire in_word  = (in_slot0 && (ofs0 < CNT_ACTIVE)) ||
                    (in_slot1 && (ofs1 < CNT_ACTIVE));
    wire phase_end = (phase == PHASE_BIT - 1);
    wire bit_end   = phase_end && (bitidx == 4'd15);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt      <= 11'd0;
            phase    <= 5'd0;
            bitidx   <= 4'd0;
            word_r   <= 16'd0;
        end else begin
            // ---- 帧周期计数 ----
            if (cnt == CNT_FRAME - 1)
                cnt <= 11'd0;
            else
                cnt <= cnt + 11'd1;

            // ---- 发送窗口内的位相位 ----
            if (!in_word) begin
                phase  <= 5'd0;
                bitidx <= 4'd0;
            end else begin
                if (phase_end) begin
                    phase <= 5'd0;
                    bitidx <= bit_end ? 4'd0 : bitidx + 4'd1;
                end else begin
                    phase <= phase + 5'd1;
                end
            end

            // ---- 每个字槽开始前锁存一次样本 ----
            // 不锁存的话 tx_left/tx_right 可能在 16 bit 发到一半时被
            // 24.414 kHz 的处理链刷新，一个字里混进两个样本（"咔"一声）。
            if (cnt == CNT_CS_HI - 1)
                word_r <= tx_left;
            else if (cnt == CNT_CS_HI + CNT_SLOT - 1)
                word_r <= tx_right;
        end
    end

    // ------------------------------------------------------------------
    //  输出（寄存器化，配 xdc 里 SLEW SLOW）
    //    sclk：发送窗口内 phase[11..23] 为高 → 上升沿在 phase 12，
    //          数据自 phase 0 起已建立 240 ns，CPHA=0 采样余量充足
    //    mosi：sclk 为低期间更新（下降沿换位），MSB 先行
    //    csn ：两个字节槽期间为低，**每帧一个下降沿** = 帧起点 = 左声道
    //          （旧版每字一个脉冲，左右就再也分不出来了，见文件头说明）
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            mcu_sclk <= 1'b0;
            mcu_csn  <= 1'b1;
            mcu_mosi <= 1'b0;
        end else begin
            mcu_sclk <= in_word && (phase >= PHASE_BIT / 2);
            mcu_csn  <= ~(in_slot0 || in_slot1);
            mcu_mosi <= word_r[15 - bitidx];
        end
    end

endmodule
