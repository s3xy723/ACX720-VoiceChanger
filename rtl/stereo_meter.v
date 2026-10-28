// ============================================================================
//  stereo_meter.v   左右声道独立电平表 + 削波检测 + 立体声（相关性）统计
//
//  为什么要有它：
//  顶层从第一天起就是"每声道实例化一条 voice_proc"，左右确实是分开算的，
//  但板上的 8 颗 LED 原来只反映**左声道**的电平（led[7:3] 是一根电平条），
//  右声道过载、左右装反、两路其实一模一样——这些在 LED 上都看不出来。
//  这个模块把"每个声道各算什么"变成看得见的东西。
//
//  统计口径
//  --------
//    电平   每个声道各自的**峰值包络**（快攻慢放：涨了立刻跟上，跌了按
//            1/64 指数回落），阈值判定"有信号"。
//    削波   各自统计 |x| >= CLIP_THRESH 的采样点数，触发后保持 0.5 秒
//            （24.414 kHz 帧率下约 12207 个采样点），闪一下就灭说明只是
//            偶发过载，常亮就是持续削顶。
//    立体声 一阶低通算出平均 |L-R| 与平均 (|L|+|R|)，当前者大于后者的
//            1/4 时点亮 led[7]。左右完全相同（单声道源、两声道路由到同
//            一路）时这个灯一定灭——它回答的是"两声道到底是不是分开的"。
//
//  所有运算都是加法/移位/比较，没有乘法，也不进 DSP48。
// ============================================================================
`timescale 1ns / 1ps

module stereo_meter #(
    parameter integer SIG_THRESH  = 16'd512,      // 约 -36 dBFS，"有信号"
    parameter integer CLIP_THRESH = 16'd32000,    // 接近满量程，"削波"
    parameter integer HOLD_SAMPLES = 14'd12207,   // 削波指示保持 ~0.5 s
    parameter integer ATT_SHIFT   = 6,            // 包络回落速度 1/2^ATT_SHIFT
    parameter integer ACC_SHIFT   = 8             // 相关性低通 1/2^ACC_SHIFT
)(
    input  wire               clk,
    input  wire               rst_n,
    input  wire               valid,        // 一个采样点有效（I2S 帧频）
    input  wire signed [15:0] din_l,
    input  wire signed [15:0] din_r,

    output reg  [15:0]        lvl_l,        // 左声道峰值包络（绝对值）
    output reg  [15:0]        lvl_r,
    output reg                sig_l,        // 有信号
    output reg                sig_r,
    output reg                clip_l,       // 削波（保持 0.5 s）
    output reg                clip_r,
    output reg                stereo        // 1 = 左右确实不同
);

    // 累加器位宽：diff/sumh 都是 17 位，左移 (ACCW-17) 后仍在 ACCW 内，
    // 再 >>ACC_SHIFT 进累加器，稳态值约等于输入本身，不会溢出。
    localparam integer ACCW = 30;

    // ------------------------------------------------------------ 绝对值
    // 注意 -32768 取绝对值是 32768，正好用满 16 位无符号，不用扩位。
    wire [15:0] a_l = din_l[15] ? (~din_l + 16'd1) : din_l;
    wire [15:0] a_r = din_r[15] ? (~din_r + 16'd1) : din_r;

    // ------------------------------------------------------------ 峰值包络
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            lvl_l <= 16'd0;
            lvl_r <= 16'd0;
        end else if (valid) begin
            lvl_l <= (a_l >= lvl_l) ? a_l : (lvl_l - (lvl_l >> ATT_SHIFT));
            lvl_r <= (a_r >= lvl_r) ? a_r : (lvl_r - (lvl_r >> ATT_SHIFT));
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sig_l <= 1'b0;
            sig_r <= 1'b0;
        end else if (valid) begin
            sig_l <= (a_l > SIG_THRESH);
            sig_r <= (a_r > SIG_THRESH);
        end
    end

    // ------------------------------------------------------------ 削波保持
    reg [13:0] hold_l, hold_r;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            hold_l <= 14'd0;
            hold_r <= 14'd0;
            clip_l <= 1'b0;
            clip_r <= 1'b0;
        end else if (valid) begin
            if (a_l >= CLIP_THRESH)            hold_l <= HOLD_SAMPLES;
            else if (hold_l != 14'd0)          hold_l <= hold_l - 14'd1;
            if (a_r >= CLIP_THRESH)            hold_r <= HOLD_SAMPLES;
            else if (hold_r != 14'd0)          hold_r <= hold_r - 14'd1;
            clip_l <= (a_l >= CLIP_THRESH) | (hold_l != 14'd0);
            clip_r <= (a_r >= CLIP_THRESH) | (hold_r != 14'd0);
        end
    end

    // ------------------------------------------------------------ 相关性
    // diff = |L - R|, sumh = |L| + |R|，各自一阶低通后比较。
    // 左右完全相同 -> diff 恒为 0 -> 灯灭；
    // 纯反相或互不相关 -> diff 平均约为 sumh 的 0.3~0.5 -> 灯亮。
    wire signed [16:0] dsub = {din_l[15], din_l} - {din_r[15], din_r};
    wire        [16:0] diff = dsub[16] ? (~dsub + 17'd1) : dsub;
    wire        [16:0] sumh = {1'b0, a_l} + {1'b0, a_r};

    // 移位后加满量程再截位，把低位丢掉，避免累加器越界
    reg [ACCW-1:0] acc_diff, acc_sum;

    wire [ACCW-1:0] diff_in = diff << (ACCW - 17);
    wire [ACCW-1:0] sum_in  = sumh << (ACCW - 17);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc_diff <= {ACCW{1'b0}};
            acc_sum  <= {ACCW{1'b0}};
        end else if (valid) begin
            acc_diff <= acc_diff - (acc_diff >> ACC_SHIFT) + (diff_in >> ACC_SHIFT);
            acc_sum  <= acc_sum  - (acc_sum  >> ACC_SHIFT) + (sum_in  >> ACC_SHIFT);
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) stereo <= 1'b0;
        else        stereo <= (acc_diff > (acc_sum >> 2));
    end

endmodule
