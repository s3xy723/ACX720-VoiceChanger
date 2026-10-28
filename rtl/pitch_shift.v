// ============================================================================
//  pitch_shift.v   变调核心：双抽头 Hann 交叉淡化延迟线
//
//  原理
//  ------
//  长度为 N = 2048 的 Hann 窗，两个抽头相距 N/2 = 1024，
//  每个抽头的权重就是窗函数在该抽头延迟位置处的取值 w(d)：
//
//        w(d) = 0.5 - 0.5*cos(pi*d / (N/2)),   d ∈ [0, N]
//
//  于是两抽头的权重恒满足 w(d) + w(d+N/2) = 1：
//      · 输出速率恒等于 fs      -> 播放时长不变
//      · 回绕处两抽头角色互换    -> 不会出现咔哒声
//      · 两个抽头的延迟同时以固定速率漂移 -> 音高改变
//
//  相位累加器 dp 为 16 位无符号小数，代表 p ∈ [0,1)。
//  每个输出样本 dp += dphase（有符号），
//  变调比          r = 1 + (N/2) * dphase / 65536 = 1 + dphase/64
//
//      dphase =  0  ->  r = 1.0   原声
//      dphase = +64 ->  r = 2.0   升八度（花栗鼠）
//      dphase = -32 ->  r = 0.5   降八度（恶魔低音）
//      dphase = +16 ->  r = 1.25  升大三度（女声化）
//      dphase = -16 ->  r = 0.75  降四度（男声化）
//
//  资源：4096 x 16 bit Block RAM（约 1~2 个 36K BRAM）+ 2 个 DSP48
//  时序：每个样本仅用 9 个 50MHz 周期，预算是 512 个周期
// ============================================================================
`timescale 1ns / 1ps

module pitch_shift #(
    parameter AW = 12                     // 地址宽度 -> 4096 深
)(
    input  wire        clk,
    input  wire        rst_n,
    input  wire        cfg_reset,         // 高电平：复位写指针与相位
    input  wire signed [15:0] dphase,     // 变调步进，见上表
    //  必须是 signed：上层传进来的是负数（降调用）。若端口声明成无符号，
    //  -32 的 9 位补码 1_1110_0000 会被零扩展成 480，相位反而往上跑，
    //  降调档会变成刺耳的升调。声明 signed 后自动符号扩展为 0xFFE0，
    //  phase += 0xFFE0 在 mod 2^16 下正好等于 phase -= 32。
    input  wire signed [15:0] din,
    input  wire        din_valid,
    output reg  signed [15:0] dout,
    output reg         dout_valid
);

    localparam SIZE = 1 << AW;            // 4096

    // ------------------------------------------------------------------
    //  延迟线：推断为 Block RAM，写口 + 读口（真双口）
    //
    //  必须显式清零。行为仿真里未初始化的 reg 阵列全是 X，而第二个抽头的
    //  延迟是 2048，会读到大片尚未写过的地址；虽然该抽头权重只有 2/65536，
    //  但 X 会污染整个乘累加结果，前 2048 个输出全是 X。真 BRAM 上电为 0
    //  不受影响，但显式初始化能让仿真结果确定，也不增加任何逻辑。
    // ------------------------------------------------------------------
    (* ram_style = "block" *) reg [15:0] mem [0:SIZE-1];

    integer mi;
    initial begin
        for (mi = 0; mi < SIZE; mi = mi + 1) mem[mi] = 16'd0;
    end

    reg [AW-1:0] waddr;
    reg [15:0]   mem_din;
    reg          wr_en;
    reg [AW-1:0] raddr;
    reg [15:0]   mem_q;

    always @(posedge clk) begin
        if (wr_en) mem[waddr] <= mem_din;
    end

    always @(posedge clk) begin
        mem_q <= mem[raddr];
    end

    wire signed [15:0] mem_q_s = mem_q;

    // ------------------------------------------------------------------
    //  相位累加器
    // ------------------------------------------------------------------
    reg [15:0] phase;

    // ------------------------------------------------------------------
    //  由相位推出两路抽头的整数延迟、小数因子和窗系数地址
    // ------------------------------------------------------------------
    wire [16:0] X  = {1'b1, 16'd0}  - {1'b0, phase};    // 65536 - dp
    wire [17:0] Y  = {2'b10, 16'd0} - {2'b00, phase};   // 131072 - dp

    // 赋给 AW 位宽的 wire，Verilog 会自动零扩展。
    // 早先写成 [10:0] / [11:0] 再取 [AW-1:0]，AW=12 时会报 part-select 越界。
    wire [AW-1:0] dly_a_int = X[16:6];          // 0   .. 1024
    wire [AW-1:0] dly_b_int = Y[17:6];          // 1024 .. 2048
    wire [5:0]    frac_a    = X[5:0];
    wire [5:0]    frac_b    = Y[5:0];

    wire [AW-1:0] base_a = waddr - dly_a_int[AW-1:0];
    wire [AW-1:0] base_b = waddr - dly_b_int[AW-1:0];

    // 窗系数 ROM 地址：ROM 512 点覆盖 d ∈ [0, 2048]
    wire [8:0]  idx_a = X[16:8];                       // 0 .. 256
    wire [9:0]  idx_b_raw = Y[17:8];                   // 256 .. 512
    wire [8:0]  idx_b = idx_b_raw[9] ? 9'd511 : idx_b_raw[8:0];

    wire [15:0] win_a_raw;
    wire [15:0] win_b_raw;
    hann_rom u_hann_a (.addr(idx_a), .dout(win_a_raw));
    hann_rom u_hann_b (.addr(idx_b), .dout(win_b_raw));

    // ------------------------------------------------------------------
    //  流水寄存器
    // ------------------------------------------------------------------
    reg signed [15:0] sa0, sa1, sb0, sb1;
    reg [15:0]        wa_r, wb_r;
    reg [5:0]         fa_r, fb_r;

    // 声明放在这里，赋值逻辑放在文件末尾（因为要用到 acc_w）。
    // 反过来的话状态机里的 dout <= dout_nxt 会报 "used before declaration"。
    reg signed [15:0] dout_nxt;

    localparam [3:0] S_IDLE = 4'd0, S_WR = 4'd1,
                     S_A0   = 4'd2, S_A1 = 4'd3,
                     S_B0   = 4'd4, S_B1 = 4'd5,
                     S_MAC  = 4'd6, S_OUT = 4'd7;

    reg [3:0] st;

    // 下一拍的读地址
    reg [AW-1:0] raddr_nxt;
    always @(*) begin
        case (st)
            S_WR:  raddr_nxt = base_a;
            S_A0:  raddr_nxt = base_a - 1'b1;
            S_A1:  raddr_nxt = base_b;
            S_B0:  raddr_nxt = base_b - 1'b1;
            default: raddr_nxt = base_b - 1'b1;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st        <= S_IDLE;
            waddr     <= {AW{1'b0}};
            phase     <= 16'd0;
            wr_en     <= 1'b0;
            mem_din   <= 16'd0;
            raddr     <= {AW{1'b0}};
            dout      <= 16'sd0;
            dout_valid<= 1'b0;
            sa0 <= 16'sd0; sa1 <= 16'sd0;
            sb0 <= 16'sd0; sb1 <= 16'sd0;
            wa_r <= 16'd0; wb_r <= 16'd0;
            fa_r <= 6'd0;  fb_r <= 6'd0;
        end else begin
            dout_valid <= 1'b0;
            wr_en      <= 1'b0;
            raddr      <= raddr_nxt;

            if (cfg_reset) begin
                st    <= S_IDLE;
                waddr <= {AW{1'b0}};
                phase <= 16'd0;
            end else begin
                case (st)
                    S_IDLE: begin
                        if (din_valid) begin
                            mem_din <= din;             // 当前样本写入 -> mem[waddr] 即 x(n)
                            wr_en   <= 1'b1;
                            // 注意：phase 故意留到 S_OUT 才更新，
                            // 保证整个处理序列期间 phase / waddr 恒定，
                            // base_a / base_b / 窗地址三者严格一致
                            wa_r    <= win_a_raw;
                            wb_r    <= win_b_raw;
                            fa_r    <= frac_a;
                            fb_r    <= frac_b;
                            st      <= S_WR;
                        end
                    end

                    S_WR:  st <= S_A0;
                    S_A0:  st <= S_A1;
                    // 此刻 mem_q == mem[base_a]
                    S_A1:  begin sa0 <= mem_q_s; st <= S_B0; end
                    // 此刻 mem_q == mem[base_a-1]
                    S_B0:  begin sa1 <= mem_q_s; st <= S_B1; end
                    // 此刻 mem_q == mem[base_b]
                    S_B1:  begin sb0 <= mem_q_s; st <= S_MAC; end
                    // 此刻 mem_q == mem[base_b-1]
                    S_MAC: begin sb1 <= mem_q_s; st <= S_OUT; end

                    S_OUT: begin
                        dout       <= dout_nxt;
                        dout_valid <= 1'b1;
                        waddr      <= waddr + 1'b1;    // 指向下一个写位置
                        phase      <= phase + dphase;  // 相位在这里漂移
                        st         <= S_IDLE;
                    end

                    default: st <= S_IDLE;
                endcase
            end
        end
    end

    // ------------------------------------------------------------------
    //  线性插值 + 加权求和
    // ------------------------------------------------------------------
    wire signed [16:0] diff_a = sa1 - sa0;
    wire signed [16:0] diff_b = sb1 - sb0;
    wire signed [6:0]  fa = {1'b0, fa_r};
    wire signed [6:0]  fb = {1'b0, fb_r};

    wire signed [23:0] step_a = (diff_a * fa) >>> 6;
    wire signed [23:0] step_b = (diff_b * fb) >>> 6;

    wire signed [17:0] tap_a  = {{2{sa0[15]}}, sa0} + step_a;
    wire signed [17:0] tap_b  = {{2{sb0[15]}}, sb0} + step_b;

    wire signed [15:0] tap_a16 = tap_a[15:0];
    wire signed [15:0] tap_b16 = tap_b[15:0];
    wire signed [16:0] wa_s    = {1'b0, wa_r};
    wire signed [16:0] wb_s    = {1'b0, wb_r};

    wire signed [31:0] prod_a = tap_a16 * wa_s;
    wire signed [31:0] prod_b = tap_b16 * wb_s;

    // 两个乘积可能各达 ±2^31，用 34 位宽做加法避免溢出
    wire signed [33:0] acc_w = {{2{prod_a[31]}}, prod_a} +
                               {{2{prod_b[31]}}, prod_b};

    // 双向饱和：只写上限会让负向过载回绕成满幅正脉冲，听起来是爆音。
    //   最大合法正值 =  32767 * 65536 =  2147418112
    //   最负合法值   = -32768 * 65536 = -2147483648
    //   dout_nxt 本身在上面已声明，这里只写赋值逻辑。
    always @(*) begin
        if (acc_w >  34'sd2147418112) dout_nxt =  16'sd32767;
        else if (acc_w < -34'sd2147418112) dout_nxt = -16'sd32768;
        else                               dout_nxt = acc_w[31:16];
    end

endmodule
