// ============================================================================
//  wm8731_config.v   WM8731 寄存器配置（2 线 MPU 接口 / I2C 兼容）
//
//  上电后 codec 处于低省电状态且为 I2S 从机默认值，必须先把下列寄存器
//  写进去才会出声。这里用纯状态机实现 I2C 主机，不占用任何 IP 核。
//
//  传输格式（每个寄存器 3 个字节）：
//      byte0 = 8'h34          器件地址 + 写位
//      byte1 = {reg_addr[6:0], reg_value[8]}    高 7 位地址 + 数据最高位
//      byte2 = reg_value[7:0]                   数据低 8 位
//
//  SCL 频率 = CLK_HZ / (4 * DIV)，默认约 100 kHz。
//  每个动作占用一个完整 SCL 周期（4 个相位），便于严格对齐时序。
// ============================================================================
`timescale 1ns / 1ps

module wm8731_config #(
    parameter          CLK_HZ = 50_000_000,
    parameter          I2C_HZ = 100_000
)(
    input  wire       clk,
    input  wire       rst_n,
    output wire       scl,
    inout  wire       sda,
    output reg        done        // 配置完成标志，高电平保持
);

    localparam integer DIV            = CLK_HZ / (I2C_HZ * 4);
    localparam integer NREG           = 11;
    localparam integer STEPS_PER_REG  = 29;      // 1(START) + 3*(8bit+ACK) + 1(STOP)
    localparam integer WAIT_MAX       = CLK_HZ / 100;  // 10 ms 上电等待

    localparam [4:0] STEP_START = 5'd0;
    localparam [4:0] STEP_STOP  = STEPS_PER_REG[4:0] - 5'd1;   // 28

    // ------------------------------------------------------------------
    //  寄存器初始化表  格式 {addr[6:0], value[8:0]}
    // ------------------------------------------------------------------
    function [15:0] cfg_word;
        input [3:0] idx;
        begin
            case (idx)
                4'd0:  cfg_word = 16'h1E00;  // R15 复位       写 0 触发软复位
                4'd1:  cfg_word = 16'h0017;  // R0  左线路输入  0dB，不静音
                4'd2:  cfg_word = 16'h0217;  // R1  右线路输入  0dB
                4'd3:  cfg_word = 16'h0479;  // R2  左耳机输出  0dB，过零更新
                4'd4:  cfg_word = 16'h0679;  // R3  右耳机输出  0dB
                4'd5:  cfg_word = 16'h0810;  // R4  模拟通路    选 DAC，不旁通
                4'd6:  cfg_word = 16'h0A00;  // R5  数字通路    不去加重，DAC 不静音
                4'd7:  cfg_word = 16'h0C00;  // R6  掉电控制    全部上电
                4'd8:  cfg_word = 16'h0E02;  // R7  数字音频口  从机 + I2S + 16bit
                4'd9:  cfg_word = 16'h1000;  // R8  采样控制    正常模式，256fs
                4'd10: cfg_word = 16'h1201;  // R9  激活        数字内核使能（必须放最后）
                default: cfg_word = 16'h0000;
            endcase
        end
    endfunction

    // ------------------------------------------------------------------
    //  时序发生器
    // ------------------------------------------------------------------
    // WAIT_MAX = 50e6/100 = 500000，需要 19 位；写成 17 位会永远计不到终值，
    // codec 配置状态机将被卡死在等待相，板上一声不出。切勿改窄。
    reg [19:0] wait_cnt;
    reg [15:0] cnt;
    reg [1:0]  ph;              // 每个 SCL 周期的 4 个相位
    reg [3:0]  reg_i;           // 当前寄存器序号 0..9
    reg [4:0]  step_i;          // 当前寄存器内的动作序号 0..28

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wait_cnt <= 17'd0;
            cnt      <= 16'd0;
            ph       <= 2'd0;
            reg_i    <= 4'd0;
            step_i   <= 5'd0;
            done     <= 1'b0;
        end else if (wait_cnt != WAIT_MAX) begin
            wait_cnt <= wait_cnt + 17'd1;
        end else if (!done) begin
            if (cnt == DIV - 1) begin
                cnt <= 16'd0;
                if (ph == 2'd3) begin
                    ph <= 2'd0;
                    if (step_i == STEP_STOP) begin
                        step_i <= 5'd0;
                        if (reg_i == NREG[3:0] - 4'd1) done  <= 1'b1;
                        else                           reg_i <= reg_i + 4'd1;
                    end else begin
                        step_i <= step_i + 5'd1;
                    end
                end else begin
                    ph <= ph + 2'd1;
                end
            end else begin
                cnt <= cnt + 16'd1;
            end
        end
    end

    // ------------------------------------------------------------------
    //  字节 / 位序号解码（不用除法，全部用范围比较）
    // ------------------------------------------------------------------
    reg [1:0] bsel;
    reg [2:0] bitnum;
    reg       is_ack;

    always @(*) begin
        bsel   = 2'd0;
        bitnum = 3'd7;
        is_ack = 1'b0;

        if (step_i >= 5'd10 && step_i <= 5'd18)
            bsel = 2'd1;
        else if (step_i >= 5'd19 && step_i <= 5'd27)
            bsel = 2'd2;

        case (step_i)
            5'd1,  5'd10, 5'd19: bitnum = 3'd7;
            5'd2,  5'd11, 5'd20: bitnum = 3'd6;
            5'd3,  5'd12, 5'd21: bitnum = 3'd5;
            5'd4,  5'd13, 5'd22: bitnum = 3'd4;
            5'd5,  5'd14, 5'd23: bitnum = 3'd3;
            5'd6,  5'd15, 5'd24: bitnum = 3'd2;
            5'd7,  5'd16, 5'd25: bitnum = 3'd1;
            5'd8,  5'd17, 5'd26: bitnum = 3'd0;
            default: begin bitnum = 3'd0; is_ack = 1'b1; end  // 9 / 18 / 27
        endcase

        if (step_i == STEP_START || step_i == STEP_STOP)
            is_ack = 1'b0;
    end

    wire [15:0] cw = cfg_word(reg_i);

    reg [7:0] txbyte;
    always @(*) begin
        case (bsel)
            2'd0: txbyte = 8'h34;                      // 器件写地址
            2'd1: txbyte = {cw[15:9], cw[8]};          // 寄存器地址 + 数据最高位
            2'd2: txbyte = cw[7:0];                    // 数据低 8 位
            default: txbyte = 8'h00;
        endcase
    end

    wire bit_val = txbyte[bitnum];

    // ------------------------------------------------------------------
    //  SCL / SDA 波形合成
    //    ph = 0 -> SCL=0      ph = 1,2 -> SCL=1      ph = 3 -> SCL=0
    // ------------------------------------------------------------------
    wire phase_active = (wait_cnt == WAIT_MAX) && !done;

    wire scl_int = (ph == 2'd1) || (ph == 2'd2);
    assign scl = (phase_active) ? scl_int : 1'b1;

    reg sda_o;
    reg sda_oe;

    always @(*) begin
        sda_o  = 1'b1;
        sda_oe = 1'b0;                                // 默认释放总线
        if (phase_active) begin
            if (step_i == STEP_START) begin
                // START：SCL 高期间 SDA 由高变低
                sda_oe = 1'b1;
                sda_o  = (ph == 2'd0 || ph == 2'd1) ? 1'b1 : 1'b0;
            end else if (step_i == STEP_STOP) begin
                // STOP：SCL 高期间 SDA 由低变高
                sda_oe = 1'b1;
                sda_o  = (ph == 2'd0 || ph == 2'd1) ? 1'b0 : 1'b1;
            end else if (is_ack) begin
                sda_oe = 1'b0;                        // 释放 SDA，读 codec 应答
                sda_o  = 1'b0;
            end else begin
                sda_oe = 1'b1;
                sda_o  = bit_val;
            end
        end
    end

    assign sda = (sda_oe && phase_active) ? sda_o : 1'bz;

endmodule
