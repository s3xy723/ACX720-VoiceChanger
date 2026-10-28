// ============================================================================
//  i2s_slave.v   I2S 从机接口（Codec 当主机，FPGA 当从机）
//
//  为什么必须是从机
//  ----------------
//  ACX720-V3 板载的是 ES8388，官方例程（ch35_CoreCourse_GHRD_I2S_RX）里
//  BCLK / LRCK 是 **输入** 到 FPGA 的（管脚表里 BCLK=U20 方向 I、LRCK=W20
//  方向 I），只有 MCLK(XCLK)=V20 由 FPGA 提供给 codec。也就是说 ES8388 跑在
//  主模式，本模块必须跟着它的 BCLK/LRCK 走。
//
//  时钟关系（本工程的配置，见 rtl/es8388_init_table.v）：
//      MCLK  = 50 MHz / 4    = 12.500 MHz   由本工程顶层产生，送给 codec
//      BCLK  = MCLK / 8      =  1.5625 MHz  codec 输出给 FPGA
//      LRCLK = MCLK / 512    = 24.414 kHz   codec 输出给 FPGA（= 采样率）
//      BCLK / LRCLK = 64，即每声道 32 个 BCLK 时隙，16 bit 有效数据
//
//  实现方式：50 MHz 过采样
//  ----------------------
//  50 MHz 对 1.5625 MHz 的 BCLK 有 32 倍过采样，完全不需要异步 FIFO。
//  BCLK / LRCK / ADCDAT 各打两三级同步器，再用边沿检测即可。
//  同步器带来 2~3 个系统周期（40~60 ns）的延迟，而 ADCDAT 在 BCLK 下降沿
//  更新、距下一个上升沿有 320 ns，余量充足。
//
//  I2S 位对齐（Philips 标准，MSB 先行）
//  -----------------------------------
//  LRCLK 跳变后的第 1 个 BCLK 上升沿是无效位（时隙 0），
//  第 2 个上升沿开始才是 MSB（bit15）…第 17 个上升沿是 LSB（bit0），
//  一个时隙共 32 个 BCLK，后 15 个全部忽略。
//  DACDAT 在 BCLK 下降沿更新，好让 codec 在上升沿锁存。
// ============================================================================
`timescale 1ns / 1ps

module i2s_slave (
    input  wire        clk,          // 50 MHz 系统时钟
    input  wire        rst_n,        // 低有效复位

    // ---- ES8388 数字音频口（BCLK / LRCK / ADCDAT 为输入）----
    input  wire        aud_bclk,
    input  wire        aud_lrclk,
    input  wire        aud_adcdat,
    output reg         aud_dacdat,

    // ---- 并行样本口 ----
    output reg  [15:0] rx_left,
    output reg  [15:0] rx_right,
    output reg         rx_strobe,    // 1 个系统时钟周期高脉冲：左右声道均已更新

    input  wire [15:0] tx_left,
    input  wire [15:0] tx_right
);

    // ------------------------------------------------------------------
    //  输入同步器（3 级，防亚稳态；第 3 级用于边沿检测）
    // ------------------------------------------------------------------
    reg [2:0] bclk_s;
    reg [2:0] lrck_s;
    reg [1:0] adcdat_s;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            bclk_s   <= 3'd0;
            lrck_s   <= 3'd0;
            adcdat_s <= 2'd0;
        end else begin
            bclk_s   <= {bclk_s[1:0],   aud_bclk};
            lrck_s   <= {lrck_s[1:0],   aud_lrclk};
            adcdat_s <= {adcdat_s[0],   aud_adcdat};
        end
    end

    // 同步器的排列是 {bclk_s[1:0], aud_bclk}，即：
    //     bclk_s[0] = 最新（第 1 级，可能有亚稳态，不能直接拿来用）
    //     bclk_s[1] = 延迟 1 拍（已同步过一级，可安全使用）
    //     bclk_s[2] = 延迟 2 拍
    // 因此边沿检测比较 [2:1]：旧值为 0、新值为 1 -> 上升沿。
    wire bclk_rise = (bclk_s[2:1] == 2'b01);
    wire bclk_fall = (bclk_s[2:1] == 2'b10);
    wire lrck_rise = (lrck_s[2:1] == 2'b01);
    wire lrck_fall = (lrck_s[2:1] == 2'b10);
    wire lrck_edge = lrck_rise | lrck_fall;

    // 取延迟 1 拍的值：在检测到跳变的那一拍，它恰好已经是跳变后的新电平，
    // 而且已经过了第一级同步器，不会出现亚稳态。0 = 左声道，1 = 右声道。
    wire lrck_lvl  = lrck_s[1];

    // ------------------------------------------------------------------
    //  时隙计数器：统计自上一次 LRCK 跳变以来发生了几个 BCLK 上升沿
    //      slot = 0  LRCK 刚跳变
    //      slot = 1  第 1 个上升沿（无效位）
    //      slot = 2..17  有效数据 bit15..bit0
    // ------------------------------------------------------------------
    reg [5:0] slot;

    // ------------------------------------------------------------------
    //  接收移位寄存器 / 发送移位寄存器
    // ------------------------------------------------------------------
    reg [15:0] rx_sr;
    reg [15:0] tx_sr;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            slot       <= 6'd0;
            rx_sr      <= 16'd0;
            tx_sr      <= 16'd0;
            rx_left    <= 16'd0;
            rx_right   <= 16'd0;
            rx_strobe  <= 1'b0;
            aud_dacdat <= 1'b0;
        end else begin
            rx_strobe <= 1'b0;

            if (lrck_edge) begin
                // 新的一帧开始了：复位时隙计数，装载要发送的字
                slot       <= 6'd0;
                rx_sr      <= 16'd0;
                tx_sr      <= lrck_lvl ? tx_right : tx_left;
                aud_dacdat <= 1'b0;
            end else if (bclk_rise) begin
                // ---- 接收：LRCK 跳变后第 2~17 个上升沿取 16 位 ----
                if (slot >= 6'd1 && slot <= 6'd16)
                    rx_sr <= {rx_sr[14:0], adcdat_s[1]};

                // ---- 收齐一整个声道：锁存 ----
                if (slot == 6'd17) begin
                    if (lrck_lvl == 1'b0) begin
                        rx_left <= rx_sr;
                    end else begin
                        rx_right  <= rx_sr;
                        rx_strobe <= 1'b1;   // 立体声样本就绪
                    end
                end

                slot <= slot + 6'd1;
            end else if (bclk_fall) begin
                // ---- 发送：下降沿更新，codec 在下一个上升沿锁存 ----
                if (slot >= 6'd1 && slot <= 6'd16) begin
                    aud_dacdat <= tx_sr[15];
                    tx_sr      <= {tx_sr[14:0], 1'b0};
                end else begin
                    aud_dacdat <= 1'b0;
                end
            end
        end
    end

endmodule
