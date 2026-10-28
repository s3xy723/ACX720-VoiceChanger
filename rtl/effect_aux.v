// ============================================================================
//  effect_aux.v   两个辅助效果：环形调制（机器人声）与回声
// ============================================================================
`timescale 1ns / 1ps

// ----------------------------------------------------------------------------
//  ring_mod : 环形调制（Ring Modulation）
//  用一个数控正弦振荡器去乘输入信号，产生经典的机器人 / 达斯维达声。
//  输出 = 0.5*干声 + 0.5*(干声 × 正弦)，保留一部分可懂度。//
//  振荡器增量 = f * 2^24 / fs,   fs = 48.828 kHz  ->  f * 343.596
// ----------------------------------------------------------------------------
module ring_mod (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [2:0]  freq_sel,        // 0..7 -> 60 / 100 / 150 / 220 /
                                        //         350 / 500 / 800 / 1200 Hz
    input  wire signed [15:0] din,
    input  wire        din_valid,
    output reg  signed [15:0] dout,
    output reg         dout_valid
);

    function [23:0] nco_inc;
        input [2:0] sel;
        begin
            case (sel)
                3'd0: nco_inc = 24'd20616;
                3'd1: nco_inc = 24'd34360;
                3'd2: nco_inc = 24'd51539;
                3'd3: nco_inc = 24'd75591;
                3'd4: nco_inc = 24'd120259;
                3'd5: nco_inc = 24'd171798;
                3'd6: nco_inc = 24'd274877;
                3'd7: nco_inc = 24'd412315;
                default: nco_inc = 24'd34360;
            endcase
        end
    endfunction

    reg [23:0] ph;

    wire [15:0] sin_raw;
    sine_rom u_sine (.addr(ph[23:16]), .dout(sin_raw));

    wire signed [15:0] sin_s = sin_raw;

    // Q15 乘法：prod >>> 15 得到真正的 干声 × 正弦，再与干声各占一半
    wire signed [31:0] prod  = din * sin_s;
    wire signed [16:0] wet   = prod >>> 15;
    wire signed [16:0] dry   = {din[15], din};
    wire signed [17:0] mixed = dry + wet;          // 取 mixed[16:1] 即除以 2

    reg signed [15:0] dout_nxt;
    always @(*) dout_nxt = mixed[16:1];            // |mixed| < 65536, 不可能溢出

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ph         <= 24'd0;
            dout       <= 16'sd0;
            dout_valid <= 1'b0;
        end else begin
            dout_valid <= 1'b0;
            if (din_valid) begin
                ph         <= ph + nco_inc(freq_sel);
                dout       <= dout_nxt;
                dout_valid <= 1'b1;
            end
        end
    end

endmodule


// ----------------------------------------------------------------------------
//  echo : 前馈梳状回声
//  dout = din + gain * x(n - D)
//  delay_len 单位为采样点，4096 点 @ 48.828 kHz = 83.9 ms
// ----------------------------------------------------------------------------
module echo #(
    parameter AW = 12
)(
    input  wire        clk,
    input  wire        rst_n,
    input  wire [AW-1:0] delay_len,
    input  wire [1:0]  gain_sel,        // 0..3 -> 0.25 / 0.375 / 0.5 / 0.625
    input  wire signed [15:0] din,
    input  wire        din_valid,
    output reg  signed [15:0] dout,
    output reg         dout_valid
);

    localparam SIZE = 1 << AW;

    (* ram_style = "block" *) reg [15:0] mem [0:SIZE-1];

    // 同 pitch_shift：显式清零，避免行为仿真里读到未初始化的 X
    integer mi;
    initial begin
        for (mi = 0; mi < SIZE; mi = mi + 1) mem[mi] = 16'd0;
    end

    reg [AW-1:0] waddr;
    reg          wr_en;
    reg [15:0]   mem_din;

    // 读地址必须先打一拍。直接把 wire 型的 raddr 接到 mem[raddr] 上时，
    // Vivado 2018.3 会报 [Synth 8-6849] ram_style="block" infeasible，
    // 转而用 LUTRAM 实现，一个声道就要吃掉约 1000 个 LUT。
    // 代价只是多一个采样点的延迟（约 20 us），对回声效果毫无影响。
    reg  [AW-1:0] raddr;
    always @(posedge clk) raddr <= waddr - delay_len;

    reg  [15:0]   mem_q;

    always @(posedge clk) begin
        if (wr_en) mem[waddr] <= mem_din;
    end

    always @(posedge clk) begin
        mem_q <= mem[raddr];
    end

    wire signed [15:0] del_s = mem_q;

    function [15:0] gain_val;
        input [1:0] sel;
        begin
            case (sel)
                2'd0: gain_val = 16'd8192;
                2'd1: gain_val = 16'd12288;
                2'd2: gain_val = 16'd16384;
                2'd3: gain_val = 16'd20480;
                default: gain_val = 16'd16384;
            endcase
        end
    endfunction

    // 组合：湿声按 Q15 加权 + 干声
    wire signed [32:0] wet_full = del_s * $signed({1'b0, gain_val(gain_sel)});
    wire signed [16:0] wet_x    = wet_full >>> 15;
    wire signed [16:0] dry_x    = {din[15], din};
    wire signed [17:0] sum      = dry_x + wet_x;

    reg signed [15:0] dout_nxt;
    always @(*) begin
        if (sum > 18'sd32767)       dout_nxt = 16'sd32767;
        else if (sum < -18'sd32768) dout_nxt = -16'sd32768;
        else                        dout_nxt = sum[15:0];
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            waddr      <= {AW{1'b0}};
            wr_en      <= 1'b0;
            mem_din    <= 16'd0;
            dout       <= 16'sd0;
            dout_valid <= 1'b0;
        end else begin
            dout_valid <= 1'b0;
            wr_en      <= 1'b0;
            if (din_valid) begin
                mem_din    <= din;
                wr_en      <= 1'b1;
                waddr      <= waddr + 1'b1;
                dout       <= dout_nxt;
                dout_valid <= 1'b1;
            end
        end
    end

endmodule
