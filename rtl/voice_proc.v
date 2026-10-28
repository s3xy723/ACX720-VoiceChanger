// ============================================================================
//  voice_proc.v   单声道效果链
//
//  四条支路常驻并行运行，由 mode 选择输出：
//      2'b00  直通      用于先验证硬件链路通不通，务必先跑这一步
//      2'b01  环形调制  机器人 / 达斯维达
//      2'b10  变调      双抽头 Hann 延迟线，保持时长不变
//      2'b11  回声      前馈梳状滤波器
//
//  每声道实例化一次。所有支路共用 50 MHz 单时钟。
// ============================================================================
`timescale 1ns / 1ps

module voice_proc #(
    parameter AW = 12
)(
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  mode,
    input  wire signed [8:0]  dphase,      // 变调步进
    input  wire [2:0]  ring_freq,          // 环形调制频率选择
    input  wire [3:0]  echo_taps,          // 回声延迟长度选择
    input  wire [1:0]  echo_gain,          // 回声增益选择
    input  wire signed [15:0] din,
    input  wire        din_valid,
    output reg  signed [15:0] dout,
    output reg         dout_valid
);

    localparam [1:0] M_BYPASS = 2'b00;
    localparam [1:0] M_RING   = 2'b01;
    localparam [1:0] M_PITCH  = 2'b10;
    localparam [1:0] M_ECHO   = 2'b11;

    // ---------------- 变调支路 ----------------
    wire signed [15:0] ps_out;
    wire               ps_valid;
    wire               ps_reset = (mode != M_PITCH);

    pitch_shift #(.AW(AW)) u_pitch (
        .clk       (clk),
        .rst_n     (rst_n),
        .cfg_reset (ps_reset),
        .dphase    (dphase),
        .din       (din),
        .din_valid (din_valid),
        .dout      (ps_out),
        .dout_valid(ps_valid)
    );

    // ---------------- 环形调制支路 ----------------
    wire signed [15:0] rm_out;
    wire               rm_valid;

    ring_mod u_ring (
        .clk       (clk),
        .rst_n     (rst_n),
        .freq_sel  (ring_freq),
        .din       (din),
        .din_valid (din_valid),
        .dout      (rm_out),
        .dout_valid(rm_valid)
    );

    // ---------------- 回声支路 ----------------
    wire signed [15:0] ec_out;
    wire               ec_valid;

    wire [AW-1:0] echo_len = {echo_taps, 8'd0};   // taps * 256 点

    echo #(.AW(AW)) u_echo (
        .clk       (clk),
        .rst_n     (rst_n),
        .delay_len (echo_len),
        .gain_sel  (echo_gain),
        .din       (din),
        .din_valid (din_valid),
        .dout      (ec_out),
        .dout_valid(ec_valid)
    );

    // ---------------- 输出选择 ----------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dout       <= 16'sd0;
            dout_valid <= 1'b0;
        end else begin
            case (mode)
                M_PITCH: begin dout <= ps_out; dout_valid <= ps_valid;  end
                M_RING:  begin dout <= rm_out; dout_valid <= rm_valid;  end
                M_ECHO:  begin dout <= ec_out; dout_valid <= ec_valid;  end
                default: begin dout <= din;    dout_valid <= din_valid; end
            endcase
        end
    end

endmodule
