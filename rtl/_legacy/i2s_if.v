// ============================================================================
//  i2s_if.v   I2S 主机接口（由 FPGA 产生全部音频时钟）
//
//  目标器件 : Xilinx Artix-7 XC7A35T (ACX720)
//  设计要点 : 全工程只有一个 50 MHz 时钟域，不需要 PLL / MMCM，
//             也不存在跨时钟域。三个音频时钟全部取自同一个 10 位计数器：
//
//        MCLK  = 50MHz / 4     = 12.500 MHz   (= 256 * fs)
//        BCLK  = 50MHz / 16    =  3.125 MHz   (=  64 * fs)
//        LRCLK = 50MHz / 1024  = 48.828 kHz   (= fs)
//
//  因为分频比全是 2 的幂（4 / 16 / 1024），三条时钟天然边沿对齐，
//  可以直接用计数器位驱动，无需任何异步握手。
//
//  I2S 时序（Philips 标准，MSB 先行，16 bit）：
//      LRCLK 低电平 = 左声道，数据在 LRCLK 跳变后的第 2 个 BCLK 上升沿开始
//      接收端在 BCLK 高电平中部采样 ADCDAT
//      发送端在 BCLK 下降沿更新 DACDAT
// ============================================================================
`timescale 1ns / 1ps

module i2s_if (
    input  wire        clk,          // 50 MHz 系统时钟
    input  wire        rst_n,        // 低有效复位

    // ---- WM8731 数字音频口 ----
    output wire        aud_mclk,
    output wire        aud_bclk,
    output wire        aud_lrclk,
    output reg         aud_dacdat,
    input  wire        aud_adcdat,

    // ---- 并行样本口 ----
    output reg  [15:0] rx_left,
    output reg  [15:0] rx_right,
    output reg         rx_strobe,    // 1 个时钟周期的高脉冲：左右声道均已更新

    input  wire [15:0] tx_left,
    input  wire [15:0] tx_right
);

    // ------------------------------------------------------------------
    //  时基：0 ~ 1023 循环
    //    cnt[9]   -> LRCLK     (半个 LRCLK 周期 = 512)
    //    cnt[8:4] -> 位槽序号   (0 ~ 31，每个声道 32 个 BCLK)
    //    cnt[3]   -> BCLK
    //    cnt[3:0] -> 单个 BCLK 内的相位 (0 ~ 15)
    // ------------------------------------------------------------------
    reg [9:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) cnt <= 10'd0;
        else        cnt <= cnt + 10'd1;
    end

    assign aud_mclk  = cnt[1];      // 50M / 4
    assign aud_bclk  = cnt[3];      // 50M / 16
    assign aud_lrclk = cnt[9];      // 50M / 1024

    wire [4:0] slot = cnt[8:4];     // 当前半个帧内的位槽 0~31
    wire [3:0] bph  = cnt[3:0];     // 位时钟内相位

    // ------------------------------------------------------------------
    //  接收通路
    //  WM8731 在 BCLK 下降沿更新 ADCDAT，因此在 BCLK 高电平的
    //  中部（bph == 12）采样，留足 12 个 50MHz 周期 = 240 ns 的建立时间。
    //  位槽 1~16 为有效数据（MSB 先行），收满 16 位后在第 17 个
    //  位槽起点锁存到左右声道寄存器。
    // ------------------------------------------------------------------
    reg [15:0] rx_sr;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_sr      <= 16'd0;
            rx_left    <= 16'd0;
            rx_right   <= 16'd0;
            rx_strobe  <= 1'b0;
        end else begin
            rx_strobe <= 1'b0;

            if ((bph == 4'd12) && (slot >= 5'd1) && (slot <= 5'd16))
                rx_sr <= {rx_sr[14:0], aud_adcdat};

            // 每个样本字在 LRCLK 半帧的第 17 个位槽起点收满并锁存
            if (cnt == 10'd272) rx_left  <= rx_sr;   // cnt[9]=0 -> 左声道
            if (cnt == 10'd784) rx_right <= rx_sr;   // cnt[9]=1 -> 右声道

            // 立体声样本就绪脉冲
            // 距下一次发送装载（cnt == 0 / 512）还有 224 个 50MHz 周期，
            // 足够后面的处理流水使用
            if (cnt == 10'd800) rx_strobe <= 1'b1;
        end
    end

    // ------------------------------------------------------------------
    //  发送通路
    //  在 BCLK 下降沿（bph == 0）移位，codec 在上升沿锁存。
    //  I2S 规定 MSB 出现在 LRCLK 跳变后的第 1 个有效位槽（即第 2 个 BCLK），
    //  所以位槽 0 输出 0，位槽 1~16 输出 MSB..LSB。
    // ------------------------------------------------------------------
    reg [15:0] tx_sr;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            tx_sr       <= 16'd0;
            aud_dacdat  <= 1'b0;
        end else if (bph == 4'd0) begin
            if (cnt == 10'd0) begin          // 左声道帧开始
                tx_sr      <= tx_left;
                aud_dacdat <= 1'b0;
            end else if (cnt == 10'd512) begin // 右声道帧开始
                tx_sr      <= tx_right;
                aud_dacdat <= 1'b0;
            end else begin
                tx_sr      <= {tx_sr[14:0], 1'b0};
                aud_dacdat <= tx_sr[15];
            end
        end
    end

endmodule
