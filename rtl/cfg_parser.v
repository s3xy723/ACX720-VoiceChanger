// -----------------------------------------------------------------------------
//  cfg_parser.v
//
//  FPGA 侧配置包解析（与 uart_audio_rx 共用同一条 UART 字节流，互不干扰）。
//
//  为什么要单独一份：原来 FPGA **完全不解析配置包**，音频帧头只认 A5 5A，
//  所以界面上的年龄参数根本进不到硬件里。这里用另一个魔数 AA 55，
//  和音频帧（A5 5A）完全不冲突，两个解析器可以并联在同一条 rx 字节流上。
//
//  包格式（长度固定、小端）：
//      AA 55 | cmd | len | data[len] | crc_lo crc_hi
//      CRC16-CCITT，init=FFFF，poly=1021，覆盖 cmd + len + data
//
//  cmd = 0x01（年龄变声参数），payload 10 字节：
//      [0..1] beta16     共振峰缩放比，Q1.14（16384 = 1.0），有符号
//      [2..3] dphase     变调相位步进，Q1.14（沿用 pitch_shift.v 的接口）
//      [4]    coef_tilt  高频倾斜，有符号 8 位（Q0.7）
//      [5]    coef_breath 气声量，无符号 8 位
//      [6]    coef_shim  微颤量，无符号 8 位
//      [7]    flags      bit0 = en（1 = 启用年龄处理）
//      [8..9] 保留（填 0）
//
//  设计取向：
//    · 解析器**只读** rx 字节流，不产生任何反压，出任何错都不影响音频通路；
//    · CRC 错 / 长度错直接丢弃，并把 err_cnt 加一（给界面显示用）；
//    · 收到的参数先落影子寄存器，CRC 通过才一次性生效 —— 避免半包参数
//      把硬件推到中间状态（比如 beta 换了而 dphase 还没换）。
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module cfg_parser (
    input  wire        clk,
    input  wire        rst_n,

    input  wire [7:0]  rx_byte,
    input  wire        rx_valid,

    output reg  signed [15:0] o_beta16,
    output reg  signed [15:0] o_dphase,
    output reg  signed [7:0]  o_coef_tilt,
    output reg         [7:0]  o_coef_breath,
    output reg         [7:0]  o_coef_shim,
    output reg                o_en,

    output reg         o_pulse,       // 参数生效时打一拍
    output reg  [15:0] o_pkt_ok,
    output reg  [15:0] o_pkt_err
);

    localparam [7:0] MAGIC0 = 8'hAA;
    localparam [7:0] MAGIC1 = 8'h55;
    localparam [7:0] CMD_AGE = 8'h01;
    localparam integer MAXLEN = 16;

    // ---------------------------------------------------------------- CRC
    function [15:0] crc16;
        input [15:0] c;
        input [7:0]  b;
        integer i;
        reg [15:0] x;
        begin
            x = c ^ {b, 8'h00};
            for (i = 0; i < 8; i = i + 1)
                x = x[15] ? ((x << 1) ^ 16'h1021) : (x << 1);
            crc16 = x;
        end
    endfunction

    // ---------------------------------------------------------------- 状态
    localparam [2:0] S_HUNT = 3'd0,   // 找 AA
                     S_M1   = 3'd1,   // 等 55
                     S_CMD  = 3'd2,
                     S_LEN  = 3'd3,
                     S_DATA = 3'd4,
                     S_CRC0 = 3'd5,
                     S_CRC1 = 3'd6;

    reg [2:0]  st;
    reg [7:0]  cmd;
    reg [7:0]  len;
    reg [3:0]  di;
    reg [15:0] crc;
    reg [7:0]  pl_buf [0:MAXLEN-1];   // 不能叫 buf：那是 Verilog 原语关键字
    reg [7:0]  crc_lo;

    // 影子寄存器
    reg signed [15:0] s_beta, s_dphase;
    reg signed [7:0]  s_tilt;
    reg        [7:0]  s_breath, s_shim;
    reg               s_en;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st <= S_HUNT; cmd <= 8'd0; len <= 8'd0; di <= 4'd0;
            crc <= 16'hFFFF; crc_lo <= 8'd0;
            o_beta16 <= 16'sd16384;      // 1.0：上电即"不变"
            o_dphase <= 16'sd0;
            o_coef_tilt <= 8'sd0;
            o_coef_breath <= 8'd0;
            o_coef_shim <= 8'd0;
            o_en <= 1'b0;
            o_pulse <= 1'b0;
            o_pkt_ok <= 16'd0;
            o_pkt_err <= 16'd0;
            s_beta <= 16'sd16384; s_dphase <= 16'sd0; s_tilt <= 8'sd0;
            s_breath <= 8'd0; s_shim <= 8'd0; s_en <= 1'b0;
        end else begin
            o_pulse <= 1'b0;

            if (rx_valid) begin
                case (st)
                S_HUNT: if (rx_byte == MAGIC0) st <= S_M1;

                S_M1: if (rx_byte == MAGIC1) st <= S_CMD;
                      else if (rx_byte != MAGIC0) st <= S_HUNT;
                      // 又收到 AA：留在 S_M1 等 55

                S_CMD: begin
                    cmd <= rx_byte;
                    crc <= crc16(16'hFFFF, rx_byte);
                    st  <= S_LEN;
                end

                S_LEN: begin
                    if (rx_byte > MAXLEN) begin
                        st <= S_HUNT;              // 长度非法，直接丢
                        o_pkt_err <= o_pkt_err + 16'd1;
                    end else begin
                        len <= rx_byte;
                        crc <= crc16(crc, rx_byte);
                        di  <= 4'd0;
                        st  <= (rx_byte == 8'd0) ? S_CRC0 : S_DATA;
                    end
                end

                S_DATA: begin
                    if (di < MAXLEN) pl_buf[di] <= rx_byte;
                    crc <= crc16(crc, rx_byte);
                    if ({4'd0, di} + 8'd1 >= len) begin
                        st <= S_CRC0;
                    end else
                        di <= di + 4'd1;
                end

                S_CRC0: begin
                    crc_lo <= rx_byte;
                    st     <= S_CRC1;
                end

                S_CRC1: begin
                    // crc 此刻是"data 全部算完"的值
                    if ({rx_byte, crc_lo} == crc) begin
                        o_pkt_ok <= o_pkt_ok + 16'd1;
                        if (cmd == CMD_AGE && len >= 8) begin
                            o_beta16      <= {pl_buf[1],  pl_buf[0]};
                            o_dphase      <= {pl_buf[3],  pl_buf[2]};
                            o_coef_tilt   <= pl_buf[4];
                            o_coef_breath <= pl_buf[5];
                            o_coef_shim   <= pl_buf[6];
                            o_en          <= pl_buf[7][0];
                            o_pulse       <= 1'b1;
                        end
                    end else begin
                        o_pkt_err <= o_pkt_err + 16'd1;
                    end
                    st <= S_HUNT;
                end

                default: st <= S_HUNT;
                endcase
            end
        end
    end

endmodule
