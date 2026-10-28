// ============================================================================
//  i2s_mirror_tx.v   给单片机的 I2S 镜像输出
//
//  干什么用
//  --------
//  把 FPGA 处理完的双声道音频（tx_left / tx_right，就是送给 ES8388 的那两路）
//  原样再串行一份，从 GPIO0 扩展口的三个引脚送给单片机（STM32F103）。
//  单片机收到后用内部双通道 DAC 还原成模拟信号，经功放推音箱。
//
//  为什么叫"镜像"
//  ----------------
//  它不自己产生 BCLK / LRCK，而是把 codec 的 BCLK / LRCK 三级同步后
//  寄存拷贝输出；数据位序列与 aud_dacdat **逐位相同**（同一个时隙计数器、
//  同一个移位寄存器、同一个装载时刻）。codec 听到什么，单片机就听到什么。
//
//  为什么不用异步 FIFO、也不用计数器自己分频
//  ----------------------------------------
//  MCLK(12.5 MHz) 是 FPGA 送给 codec 的，codec 的 BCLK/LRCK 由它整数分频
//  而来；本模块的 BCLK/LRCK 是 codec 输入线的同步寄存拷贝，装载事件又是
//  codec 的 LRCK 边沿 —— 整条链路**零时钟域穿越、零速率匹配**。
//  如果自己用计数器从 12.5 MHz 分频出 BCLK/LRCK，标称频率虽然一样，但
//  与 codec 的 LRCK 相位关系不可控，可能出现左右声道错帧（详见
//  mcu/README.md 里的分析），所以不这么做。
//
//  时序（与 rtl/i2s_slave.v 的发送路径逐行同构，Philips 标准）：
//      LRCK 跳变后的第 1 个 BCLK 是无效位，第 2~17 个 BCLK 依次是
//      bit15..bit0，其余时隙补 0；数据在 BCLK 下降沿更新，接收方在
//      上升沿采样。输出比 codec 总线整体晚 2~3 个系统周期（40~60 ns），
//      三路信号同域同延，自洽，对接收方只是多了不到 60 ns 延迟。
//
//  单片机侧对应配置（STM32）：
//      I2S2 从机 + Philips 标准 + 16bit 数据 + 32bit 通道
//      （STM32 库里叫 I2S_DataFormat_16bextended，DR 是 16 位的，
//        每声道触发一次 RXNE —— 与 RM0008 的描述一致）
// ============================================================================
`timescale 1ns / 1ps

module i2s_mirror_tx (
    input  wire        clk,          // 50 MHz 系统时钟
    input  wire        rst_n,        // 低有效复位

    // ---- 来自 ES8388 的 I2S 总线（本工程里 BCLK/LRCK 是输入）----
    input  wire        aud_bclk,     // 1.5625 MHz
    input  wire        aud_lrclk,    // 24.414 kHz

    // ---- 处理后的样本（与 i2s_slave 的 tx_left / tx_right 同源）----
    input  wire [15:0] tx_left,
    input  wire [15:0] tx_right,

    // ---- 给单片机的 I2S 总线（输出，接 STM32 的 I2S2）----
    output reg         mcu_bclk,     // = aud_bclk  的同步寄存拷贝
    output reg         mcu_lrck,     // = aud_lrclk 的同步寄存拷贝
    output reg         mcu_dout      // = aud_dacdat 的同序列拷贝
);

    // ------------------------------------------------------------------
    //  输入同步器（与 i2s_slave.v 完全相同的三级结构）
    // ------------------------------------------------------------------
    reg [2:0] bclk_s;
    reg [2:0] lrck_s;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            bclk_s <= 3'd0;
            lrck_s <= 3'd0;
        end else begin
            bclk_s <= {bclk_s[1:0], aud_bclk};
            lrck_s <= {lrck_s[1:0], aud_lrclk};
        end
    end

    // 排列是 {旧..新}：索引 0 最新（可能亚稳态）、索引 2 延迟两拍。
    // 边沿检测比较 [2:1]：旧 0 新 1 -> 上升沿。
    wire bclk_rise = (bclk_s[2:1] == 2'b01);
    wire bclk_fall = (bclk_s[2:1] == 2'b10);
    wire lrck_rise = (lrck_s[2:1] == 2'b01);
    wire lrck_fall = (lrck_s[2:1] == 2'b10);
    wire lrck_edge = lrck_rise | lrck_fall;
    wire lrck_lvl  = lrck_s[1];      // 0 = 左声道，1 = 右声道

    // ------------------------------------------------------------------
    //  时隙计数器 + 发送移位寄存器（i2s_slave.v 发送路径的逐行同构拷贝）
    // ------------------------------------------------------------------
    reg [5:0]  slot;
    reg [15:0] tx_sr;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            slot     <= 6'd0;
            tx_sr    <= 16'd0;
            mcu_bclk <= 1'b0;
            mcu_lrck <= 1'b0;
            mcu_dout <= 1'b0;
        end else begin
            // ---- 时钟线：同步后的寄存拷贝（与数据同域、同延时，自洽）----
            mcu_bclk <= bclk_s[2];
            mcu_lrck <= lrck_s[2];

            // ---- 数据线：与 aud_dacdat 同一个 bit 序列 ----
            if (lrck_edge) begin
                // 新的半帧开始：复位时隙计数，装载要发送的字
                // （此刻 tx_left/tx_right 里装的是上一帧处理完的样本，
                //  与 i2s_slave 的装载时刻完全相同，所以两路串行流一致）
                slot     <= 6'd0;
                tx_sr    <= lrck_lvl ? tx_right : tx_left;
                mcu_dout <= 1'b0;                 // 时隙 0 = 无效位
            end else if (bclk_rise) begin
                // 上升沿只推进时隙计数；发送在下降沿更新
                slot <= slot + 6'd1;
            end else if (bclk_fall) begin
                // 下降沿更新，接收方在下一个上升沿锁存
                if (slot >= 6'd1 && slot <= 6'd16) begin
                    mcu_dout <= tx_sr[15];         // MSB 先行
                    tx_sr    <= {tx_sr[14:0], 1'b0};
                end else begin
                    mcu_dout <= 1'b0;              // 时隙 17..31 补 0
                end
            end
        end
    end

endmodule
