// ============================================================================
//  age_tint.v   年龄变声的"音质着色"级（时域，接在变调之后）
//
//  四件事里靠后的三件：
//      气声（声门闭合不全）   -> 与语音包络同步的噪声，不是常亮底噪
//      微颤（神经肌肉控制差） -> 慢随机增益起伏
//      高频倾斜（组织松弛）   -> 一阶搁架，3.5 kHz 以上抬/压
//  基频由 pitch_shift.v、共振峰由 age_core.v 负责，都不在本模块。
//
//  ★ 系数一律由上位机算好（host/age_model.py::pack），这里只做定点乘加。
//    dB→线性、百分比→增益这类换算硬件里做不了，参数也得能随时改。
//
//  ★★ en=0 必须**逐位直通**。对应"同一年龄→同一年龄"，此时系数全为 0；
//     只要有一条支路没关干净（比如噪声照旧往输出里加），结果就不再等于输入。
//     所以直通路径不经过任何乘法，只走延迟对齐，切换档位不会错拍。
//
//  定点格式（与 host/age_model.py::pack() 一一对应）
//      coef_breath  Q4.12   0 .. 8    气声噪声增益
//      coef_shim    Q1.14   0 .. 2    微颤深度
//      coef_tilt    Q2.13  -4 .. +4   (线性增益 - 1)
//
//  时序：每样本 2 拍，预算 2048 拍（24.414 kHz @ 50 MHz），余量 1000 倍。
// ============================================================================
`timescale 1ns / 1ps

module age_tint (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               en,              // 0 = 逐位直通

    input  wire signed [15:0] coef_breath,     // Q4.12
    input  wire signed [15:0] coef_shim,       // Q1.14
    input  wire signed [15:0] coef_tilt,       // Q2.13

    input  wire signed [15:0] din,
    input  wire               din_valid,
    output reg  signed [15:0] dout,
    output reg                dout_valid
);

    // ---------------------------------------------------------------- 饱和
    // ★ 入参固定 36 位有符号。用拼接 {N{sign}, v} 做符号扩展是**无符号**的，
    //   负数会被零扩展成巨大的正数，接着被这里饱和成 +32767 —— 高频搁架会
    //   整个变成直流偏移。所有调用一律用 $signed() 显式转换。
    function signed [15:0] sat16;
        input signed [35:0] v;
        begin
            if (v > 36'sd32767)       sat16 = 16'sd32767;
            else if (v < -36'sd32768) sat16 = -16'sd32768;
            else                      sat16 = v[15:0];
        end
    endfunction

    // ---------------------------------------------------------------- 噪声源
    // 24 位最大长度 LFSR（x^24+x^23+x^22+x^17+1）。白噪声给气声，
    // 同一序列平滑后给微颤 —— 一路出两种纹理，省一个发生器。
    reg  [23:0] lfsr;
    wire [23:0] lfsr_nxt = {lfsr[22:0],
                            lfsr[23] ^ lfsr[22] ^ lfsr[21] ^ lfsr[16]};
    wire signed [15:0] rnd_white = $signed({1'b0, lfsr[22:8]}) - 16'sd16384;

    // 一阶平滑 ≈ 20 Hz @ 24.414 kHz（alpha = 1/512）。平滑后幅度只剩 1/32
    // 左右（实测 std 约 170/16384），乘 32 补回到 ±0.33，微颤才够听。
    reg  signed [23:0] shim_lp;
    wire signed [23:0] shim_lp_nxt =
        shim_lp + (({{8{rnd_white[15]}}, rnd_white} - shim_lp) >>> 9);
    wire signed [23:0] rnd_slow = shim_lp_nxt <<< 5;

    // ---------------------------------------------------------------- 包络
    // 一阶低通 alpha=1/256 ≈ 10.5 ms。气声必须跟着包络走；常亮噪声听起来
    // 是录音底噪，不是"声门漏气"。
    reg        [15:0] env;
    wire        [15:0] abs_in  = din[15] ? (~din + 16'd1) : din;
    wire        [15:0] env_nxt = env - (env >> 8) + (abs_in >> 8);

    // ---------------------------------------------------------------- 高频搁架
    // 一阶高通 3.5 kHz @24.414 kHz：a = 1-exp(-2*pi*3500/24414) = 0.5934，
    // Q0.16 -> 38887。系数与采样率绑定，换 fs 必须重算。
    reg  signed [15:0] x_prev, hp;
    wire signed [17:0] hp_d   = $signed(hp) + $signed(din) - $signed(x_prev);
    wire signed [35:0] hp_mul = hp_d * 36'sd38887;
    wire signed [15:0] hp_nxt = sat16($signed(hp_mul[35:16]));

    // ---------------------------------------------------------------- 一级流水
    reg               v1, v2;
    reg signed [15:0] x1, x2;
    reg signed [35:0] sum1;        // 一级算完的合计，二级饱和输出
    reg signed [15:0] hp1;
    reg signed [23:0] slow1;
    reg        [15:0] env1;
    reg signed [15:0] w1;

    // 一级组合：tilt 项（hp × Q2.13）
    wire signed [35:0] tilt_c =
        ($signed(hp1) * $signed(coef_tilt)) >>> 13;
    // 一级组合：shim 项（x × (1 + slow×Q1.14/2^14)）
    wire signed [35:0] shim_g = 36'sd16384 +
        (($signed(slow1[23:8]) * $signed(coef_shim)) >>> 6);
    wire signed [35:0] shim_c = ($signed(x1) * shim_g) >>> 14;
    // 二级组合：气声（env × white × Q4.12）
    wire signed [35:0] br_c =
        ((($signed({1'b0, env1[14:0]}) * $signed(w1)) >>> 14) *
         $signed(coef_breath)) >>> 12;

    wire signed [35:0] mix = tilt_c + shim_c + br_c;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            lfsr    <= 24'hABCDE1;         // 非零种子，否则最大长度序列卡死
            shim_lp <= 24'sd0;
            env     <= 16'd0;
            x_prev  <= 16'sd0;
            hp      <= 16'sd0;
            v1 <= 1'b0; v2 <= 1'b0;
            x1 <= 16'sd0; x2 <= 16'sd0; sum1 <= 36'sd0;
            hp1 <= 16'sd0; slow1 <= 24'sd0; env1 <= 16'd0; w1 <= 16'sd0;
            dout <= 16'sd0;
            dout_valid <= 1'b0;
        end else begin
            v1 <= din_valid;
            v2 <= v1;
            dout_valid <= v2;

            // ---- 一级：更新状态并锁存
            if (din_valid) begin
                lfsr    <= lfsr_nxt;
                shim_lp <= shim_lp_nxt;
                env     <= env_nxt;
                x_prev  <= din;
                hp      <= hp_nxt;
                x1      <= din;
                hp1     <= hp_nxt;
                slow1   <= rnd_slow;
                env1    <= env_nxt;
                w1      <= rnd_white;
            end
            // ---- 一级 → 二级：算完合计，并让直通路径同步延迟
            if (v1) begin
                sum1 <= mix;
                x2   <= x1;
            end

            // ---- 二级：饱和（或直通）输出
            if (v2)
                dout <= en ? sat16(sum1) : x2;
        end
    end

endmodule
