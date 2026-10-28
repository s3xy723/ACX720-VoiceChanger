// ============================================================================
//  separation_core.v   人声/伴奏分离内核（中置掩蔽 + 双基频 + 梳状 + OLA）
//                      ★ 2026-10-21：输出语义只有一种 —— 左=人声、右=伴奏
//
//  把 host/vocal_split.py 那条链路搬进硬件。算法与全部定点参数都由
//  tool/sim_separation.py 标定过（10/10 PASS）——**改任何一个移位量都要重跑它**。
//
//  一帧的信号流
//  ------------
//      mid = (L+R)/2   side = (L-R)/2          （人声居中 => 能量都落在 mid 里）
//      FFT(mid)  FFT(side)
//      c = (|mid|-|side|)/|mid|     V = c*mid   （中置掩蔽，保留相位）
//      |V|^2 --IFFT--> 自相关 --> 低 / 高两个迟滞区间各找一条基频
//      谐波梳竞争 --> Vm / Vf
//      IFFT(Vm) IFFT(Vf) --> 加窗 --> 重叠相加到 acc_m / acc_f
//      dout_l = acc_v                            （左 = 人声）
//      dout_r = mid - acc_s                      （右 = 伴奏，acc_s 过了低频保护）
//
//  时序
//  ----
//  fs = 24.414 kHz、帧跳 256 => 一帧 524,288 个系统时钟。
//  一帧要 5 次 1024 点变换（各 25,600 拍）加逐点处理，合计约 15 万拍，
//  **余量 3.5 倍**，所以一个蝶形单元串行跑五次就够，不需要并行。
//
//  延迟
//  ----
//  固定 DELAY = 1280 点（52.4 ms）。必须大于 1024 + 处理耗时（约 76 点）：
//  输出位置 p 要等覆盖它的四帧全部写进 acc 之后才能取，否则读到半截结果。
//
//  标定出来的定点参数（别凭感觉改）
//  ------------------------------
//  内部 24 位；FFT 输入 = 16 位音频左移 1 位；|V|^2 右移 18（再小 acf[0] 会溢出）；
//  掩蔽除法 15 步（16 步会得到 Q16，num==den 时商直接翻倍）；
//  OLA 右移 1 位补回输入左移的那一位，再乘 2/3 补回四帧 Hann² 之和 1.5。
// ============================================================================
`timescale 1ns / 1ps

module separation_core #(
    parameter DELAY = 1280
)(
    input  wire         clk,
    input  wire         rst_n,
    input  wire         en,                 // 1 = 走分离，0 = 直通

    input  wire signed [15:0] din_l,
    input  wire signed [15:0] din_r,
    input  wire         din_valid,

    output reg  signed [15:0] dout_l,
    output reg  signed [15:0] dout_r,
    output reg          dout_valid,

    output reg  [9:0]   f0_m_lag,           // 音高 1（自相关迟滞拍数）
    output reg  [9:0]   f0_f_lag,           // 音高 2
    output reg  [1:0]   who,                // 0 无 / 1 只有音高1 / 2 只有音高2 / 3 两个都在
    output reg          busy
);

    localparam NBIN = 512;
    localparam HARM_MAX = 40;
    localparam [4:0] PW_SHIFT  = 5'd18;
    localparam [4:0] OLA_SHIFT = 5'd1;
    localparam [15:0] OLA_Q15  = 16'd21845;   // 2/3

    // 迟滞区间：低音域 70..175 Hz、高音域 165..500 Hz（分界 165 Hz，两边故意重叠）。
    // 只用来「一次找两个同时存在的音高」（对唱场合），与性别无关。
    localparam [9:0] LAG_M_LO = 10'd139, LAG_M_HI = 10'd348;
    localparam [9:0] LAG_F_LO = 10'd48,  LAG_F_HI = 10'd157;

    localparam [4:0] S_IDLE  = 5'd0,
                     S_LDMID = 5'd1,  S_FMID  = 5'd2,  S_RDMID = 5'd3,
                     S_LDSD  = 5'd4,  S_FSD   = 5'd5,  S_RDSD  = 5'd6,
                     S_MASK  = 5'd7,
                     S_LDPW  = 5'd8,  S_FACF  = 5'd9,  S_RDACF = 5'd10,
                     S_PKM   = 5'd11, S_PKF   = 5'd12, S_SIMPLE = 5'd13,
                     S_PKM2  = 5'd14, S_SOLO  = 5'd15,
                     S_CLRC  = 5'd16, S_CMBM  = 5'd17, S_CMBF  = 5'd18,
                     S_LDVM  = 5'd19, S_FIM   = 5'd20, S_RDVM  = 5'd21,
                     S_LDVF  = 5'd22, S_FIF   = 5'd23, S_RDVF  = 5'd24;

    // ---------------------------------------------------------------- 存储
    (* ram_style = "block" *) reg signed [15:0] buf_l [0:2047];
    (* ram_style = "block" *) reg signed [15:0] buf_r [0:2047];
    (* ram_style = "block" *) reg signed [23:0] acc_m [0:2047];
    (* ram_style = "block" *) reg signed [23:0] acc_f [0:2047];
    (* ram_style = "block" *) reg signed [23:0] sp_re [0:511];
    (* ram_style = "block" *) reg signed [23:0] sp_im [0:511];
    (* ram_style = "block" *) reg [23:0] sd_mag [0:511];
    (* ram_style = "block" *) reg signed [23:0] acf   [0:1023];
    (* ram_style = "block" *) reg [1:0] cmb_m [0:511];
    (* ram_style = "block" *) reg [1:0] cmb_f [0:511];

    integer q;
    initial begin
        for (q = 0; q < 2048; q = q + 1) begin
            buf_l[q] = 16'sd0; buf_r[q] = 16'sd0;
            acc_m[q] = 24'sd0; acc_f[q] = 24'sd0;
        end
        for (q = 0; q < 512; q = q + 1) begin
            sp_re[q] = 24'sd0; sp_im[q] = 24'sd0; sd_mag[q] = 24'd0;
            cmb_m[q] = 2'd0; cmb_f[q] = 2'd0;
        end
        for (q = 0; q < 1024; q = q + 1) acf[q] = 24'sd0;
    end

    // ---------------------------------------------------------------- 寄存器
    reg [4:0]  state;
    reg [9:0]  idx;
    reg [1:0]  ph;
    // 读延迟：fft1024 的 rd_re / rd_im 是**两级**寄存器输出
    //   （d0_re <= m_re[pa]  ->  rd_re <= d0_re），
    // 而且 pa 是 busy 复用的，地址本身还要再过一级 mux。合起来从"给出
    // rd_addr"到"rd_re 稳定"是**三拍**，所以读状态必须走四段：
    //   rph=0 给地址 / rph=1 等 / rph=2 等 / rph=3 取数。
    // 少等一拍读到的是上一个地址的值，整条谱会被循环移一个 bin
    // （低频就是 23.8 Hz 的偏差），基频估计跟着全错 ——
    // 这个 bug 就是靠"RTL 谱 = 模型谱整体右移一位"才抓出来的。
    // 注意：合成段（S_RDVM/S_RDVF）比别的读状态多一段（rph==4），因为 acc
    // 是 BRAM、读要打一拍，读改写必须是"先读 -> 后写"两步。
    reg [2:0]  rph;
    reg [4:0]  dstep;
    reg [5:0]  hidx;
    reg [10:0] wp;
    reg [10:0] win_start;
    reg        warm;                   // 缓冲区至少写满过一轮才开分析
    reg        frame_req, frame_busy;
    reg [10:0] buf_rd_addr;

    // acc_m / acc_f 的两个同步读口。BRAM 没有异步读，两边都得打一拍：
    //   acc_m_o / acc_f_o : B 口，给输出用。read-first，所以"读完即清"这一拍
    //                       读到的正是清零**前**的累加值 —— 就是要扣掉的成分。
    //   acc_m_a / acc_f_a : A 口，给合成用，读 -> 加 -> 写回（所以要两段）。
    reg signed [23:0] acc_m_o = 24'sd0, acc_f_o = 24'sd0;
    reg signed [23:0] acc_m_a = 24'sd0, acc_f_a = 24'sd0;

    // 谱 / side 幅度 / 自相关的同步读。这三块也进了 BRAM，所以所有"查表"
    // 的地方都改成先读进下面这几个寄存器（读地址见 sp_raddr / acf_raddr）。
    reg signed [23:0] sp_re_r = 24'sd0, sp_im_r = 24'sd0;
    reg        [23:0] sd_mag_r = 24'd0;
    reg signed [23:0] acf_r = 24'sd0;

    reg signed [23:0] mid_v, side_v;
    reg [23:0] mag_m;
    reg signed [24:0] rem_r;
    reg [15:0] c_q15;
    reg signed [23:0] pw_r;

    reg signed [23:0] acf0, peak_m, peak_f;
    reg [9:0] lag_m, lag_f, lag_m2, scan_l;
    reg signed [23:0] scan_best;
    reg [9:0] scan_best_lag;

    reg [8:0]  solo_pos;
    reg signed [23:0] solo_p, solo_bg;
    reg [3:0] solo_i;

    reg [1:0] who_r;
    reg [9:0] lm_r, lf_r;

    wire [10:0] out_idx = wp - DELAY[10:0];

    // ---------------------------------------------------------------- FFT / ROM
    reg         f_start, f_inv, f_wen;
    reg  [9:0]  f_waddr, f_raddr;
    reg  signed [23:0] f_wre, f_wim;
    wire        f_busy, f_done;
    wire signed [23:0] f_rre, f_rim;

    fft1024 u_fft (
        .clk(clk), .rst_n(rst_n),
        .start(f_start), .inverse(f_inv), .busy(f_busy), .done(f_done),
        .wr_en(f_wen), .wr_addr(f_waddr), .wr_re(f_wre), .wr_im(f_wim),
        .rd_addr(f_raddr), .rd_re(f_rre), .rd_im(f_rim)
    );

    wire [15:0] win_val;
    win1024 u_win (.addr(idx[9:0]), .w(win_val));


    wire [9:0]  rc_lag = (state == S_CMBF) ? lf_r
                       : ((state == S_SOLO) ? lag_m : lm_r);
    wire [23:0] rc_q8;
    pitch_recip u_rc (.lag(rc_lag), .f0_q8(rc_q8));

    // ---------------------------------------------------------------- 函数

    function [23:0] amax_beta;
        input signed [23:0] re;
        input signed [23:0] im;
        reg [23:0] a, b, hi, lo;
        begin
            a  = re[23] ? (~re + 24'd1) : re;
            b  = im[23] ? (~im + 24'd1) : im;
            hi = (a >= b) ? a : b;
            lo = (a >= b) ? b : a;
            amax_beta = hi + (lo >> 2) + (lo >> 3);   // max + 0.375*min
        end
    endfunction

    function [15:0] recip4;
        input [2:0] s;
        begin
            case (s)
                3'd1: recip4 = 16'd32767;
                3'd2: recip4 = 16'd16384;
                3'd3: recip4 = 16'd10923;
                3'd4: recip4 = 16'd8192;
                default: recip4 = 16'd0;
            endcase
        end
    endfunction

    function signed [15:0] sat16;
        input signed [23:0] v;
        begin
            sat16 = (v > 24'sd32767) ? 16'sd32767
                  : ((v < -24'sd32768) ? -16'sd32768 : v[15:0]);
        end
    endfunction

    // 低频保护：一阶高通幅频响应 1/(1+(120/f)^2)，按频点查表（Q15）。
    // 只列到 bin 32（763 Hz，hp=0.944），更高的一律 1.0 —— 再往上差不到 5%，
    // 不值得为它多占 480 个表项。fc 与 host 的 bass_protect_hz=120 对齐；
    // 这一条在真实歌曲上值 +9.3 dB 的伴奏 SDR（关掉它贝斯会被一起扣掉）。
    function [15:0] hp_of;
        input [8:0] k;
        begin
            case (k)
            9'd0  : hp_of = 16'd0;
            9'd1  : hp_of = 16'd1244;
            9'd2  : hp_of = 16'd4468;
            9'd3  : hp_of = 16'd8590;
            9'd4  : hp_of = 16'd12684;
            9'd5  : hp_of = 16'd16275;
            9'd6  : hp_of = 16'd19233;
            9'd7  : hp_of = 16'd21600;
            9'd8  : hp_of = 16'd23475;
            9'd9  : hp_of = 16'd24961;
            9'd10 : hp_of = 16'd26144;
            9'd11 : hp_of = 16'd27094;
            9'd12 : hp_of = 16'd27865;
            9'd13 : hp_of = 16'd28496;
            9'd14 : hp_of = 16'd29017;
            9'd15 : hp_of = 16'd29451;
            9'd16 : hp_of = 16'd29816;
            9'd17 : hp_of = 16'd30126;
            9'd18 : hp_of = 16'd30391;
            9'd19 : hp_of = 16'd30618;
            9'd20 : hp_of = 16'd30815;
            9'd21 : hp_of = 16'd30987;
            9'd22 : hp_of = 16'd31137;
            9'd23 : hp_of = 16'd31270;
            9'd24 : hp_of = 16'd31387;
            9'd25 : hp_of = 16'd31491;
            9'd26 : hp_of = 16'd31583;
            9'd27 : hp_of = 16'd31667;
            9'd28 : hp_of = 16'd31741;
            9'd29 : hp_of = 16'd31809;
            9'd30 : hp_of = 16'd31870;
            9'd31 : hp_of = 16'd31925;
            9'd32 : hp_of = 16'd31976;
            default: hp_of = 16'd32767;
            endcase
        end
    endfunction

    // |V|^2 = (re^2 + im^2) >> PW_SHIFT。
    // 注意参数是**同步读出来的寄存器值**而不是数组地址：sp 搬进 BRAM 之后没有
    // 异步读了，直接写 sp_re[a] 会让 Vivado 又把整块推回 LUTRAM。
    function [23:0] pw_at_r;
        input signed [23:0] re;
        input signed [23:0] im;
        reg signed [47:0] p;
        begin
            p = re * re + im * im;
            pw_at_r = (p >>> PW_SHIFT);
        end
    endfunction

    // 是不是 lag_f 的整数倍鬼影（2T、3T…8T）。
    // 周期 T 的信号在这些位置一样出峰，只挖 2T 的话 3T 立刻补位 ——
    // 实测那样会把女声独唱误判成两人齐唱。
    function is_ghost;
        input [9:0] lag;
        input [9:0] lf;
        reg [9:0] tol;
        begin
            tol = (lf >> 3) > 10'd3 ? (lf >> 3) : 10'd3;
            is_ghost = (({1'b0, lag} + tol) >= {1'b0, lf} * 2 &&
                        ({1'b0, lag} <= {1'b0, lf} * 2 + tol)) ||
                       (({1'b0, lag} + tol) >= {1'b0, lf} * 3 &&
                        ({1'b0, lag} <= {1'b0, lf} * 3 + tol)) ||
                       (({1'b0, lag} + tol) >= {1'b0, lf} * 4 &&
                        ({1'b0, lag} <= {1'b0, lf} * 4 + tol)) ||
                       (({1'b0, lag} + tol) >= {1'b0, lf} * 5 &&
                        ({1'b0, lag} <= {1'b0, lf} * 5 + tol)) ||
                       (({1'b0, lag} + tol) >= {1'b0, lf} * 6 &&
                        ({1'b0, lag} <= {1'b0, lf} * 6 + tol)) ||
                       (({1'b0, lag} + tol) >= {1'b0, lf} * 7 &&
                        ({1'b0, lag} <= {1'b0, lf} * 7 + tol)) ||
                       (({1'b0, lag} + tol) >= {1'b0, lf} * 8 &&
                        ({1'b0, lag} <= {1'b0, lf} * 8 + tol));
        end
    endfunction

    // ---------------------------------------------------------------- 组合线
    // bl_s / br_s 现在是**寄存器**（BRAM 同步读），所以装载状态进入时要先空
    // 一拍让地址跟上。见 S_LDMID 里的注释。
    reg signed [16:0] bl_s, br_s;
    // 输出侧的同步读。out_idx 每 2048 个样本才变一次，A 口又每拍都在读它，
    // 所以取数那拍早就稳了。
    reg signed [15:0] buf_l_o, buf_r_o;
    // 延迟到输出位置的 mid = (L+R)/2。直接用已经延迟好的 buf 输出算，
    // 不需要第三块 BRAM。宽度对齐到 sat16 需要的 24 位有符号。
    wire signed [16:0] mid_sum = buf_l_o + buf_r_o;
    wire signed [23:0] val_mid = {{8{mid_sum[16]}}, mid_sum[16:1]};

    // 谐波位置：必须四舍五入，截断会让梳子整体左偏近一个 bin，
    // 低频一个 bin 就是 24 Hz，偏一格足以把另一个人的基波当成自己的谐波吃掉
    wire [31:0] comb_mul = hidx * rc_q8;
    wire [9:0]  combo_pos = (comb_mul + 32'd128) >> 8;

    // 实偶谱映射：写 1024 点时，后半段取共轭镜像
    wire [8:0]  mirror_idx = idx[9] ? (11'd1024 - idx) : idx[8:0];
    wire        idx_nyq    = (idx == 10'd512);

    // ---------------------------------------------------------------- 掩蔽值
    // 2026-10-21 改版：输出语义只有一种 —— **左 = 人声，右 = 伴奏**。
    // 原来这里算"男声一把梳 / 女声一把梳"再按 who 分派，现在两只梳子只
    // 用来**覆盖两个音高**（男女对唱时会同时存在），所以直接取并集：
    //
    //   mask_v = max(cmb_m, cmb_f) / 2      （每格 0 / 0.5 / 1.0，Q15）
    //   mask_s = mask_v * hp[bin]           （扣伴奏用的那一份，过低频保护）
    //
    // 两个累加器的用途也随之改变（BRAM 数量不变，还是两块）：
    //   acc_m -> acc_v : OLA(IFFT(mask_v * V))   人声     -> 左声道
    //   acc_f -> acc_s : OLA(IFFT(mask_s * V))   扣伴奏用 -> 右声道 = mid - acc_s
    wire [1:0]  cmb_uni = (cmb_m[mirror_idx] > cmb_f[mirror_idx])
                        ? cmb_m[mirror_idx] : cmb_f[mirror_idx];
    wire [16:0] mv_t    = cmb_uni * 17'd16384;
    wire [15:0] mask_v  = (mv_t > 17'd32767) ? 16'd32767 : mv_t[15:0];
    // 低频保护的响应查 hp_of()（Q15，与 host 的 bass_protect_hz=120 对齐）
    wire [15:0] hp_q15  = hp_of(mirror_idx);
    wire [31:0] ms_t    = mask_v * hp_q15;
    wire [15:0] mask_s  = ms_t[30:15];

    // ---------------------------------------------------------------- 乘法位宽
    // Verilog 里 a*b 的位宽只由**操作数**决定，跟赋值目标多宽没关系
    // （这点跟 VHDL 不一样）。24×17 若不显式扩到位就会被截回 24 位，
    // 加窗那一步会直接把谱打没 —— 实测 mid 谱只剩 ±100（应该是 ±1e5）。
    // 所以每个乘法都先落到一根显式加宽的线上，再做移位。
    wire signed [16:0] win_s    = $signed({1'b0, win_val});
    wire signed [40:0] m_win    = mid_v  * win_s;     // 分析窗
    wire signed [40:0] s_win    = side_v * win_s;
    wire signed [40:0] v_mul_re = sp_re_r * $signed({1'b0, c_q15});
    wire signed [40:0] v_mul_im = sp_im_r * $signed({1'b0, c_q15});
    wire signed [40:0] mk_re    = sp_re_r * $signed({1'b0, mask_v});
    wire signed [40:0] mk_im    = sp_im_r * $signed({1'b0, mask_v});
    wire signed [40:0] kf_re    = sp_re_r * $signed({1'b0, mask_s});
    wire signed [40:0] kf_im    = sp_im_r * $signed({1'b0, mask_s});
    wire signed [40:0] o_mul    = f_rre * win_s;      // 合成窗
    wire signed [24:0] o_win    = (o_mul + 41'sd16384) >>> 15;
    wire signed [41:0] o_ola    = o_win * $signed({1'b0, OLA_Q15});

    // 当前同步读出来的那一点的 |V|^2（S_LDPW / S_SOLO 用）
    wire [23:0] sp_pw = pw_at_r(sp_re_r, sp_im_r);

    function signed [23:0] sat24;
        input signed [41:0] v;
        begin
            sat24 = (v >  42'sd8388607)  ?  24'sd8388607
                  : ((v < -42'sd8388608) ? -24'sd8388608 : v[23:0]);
        end
    endfunction

    // 本帧在 acc 里的落点
    wire [10:0] win_idx = (win_start + {1'b0, idx}) & 11'd2047;

    // solo 判据的取样偏移
    reg signed [10:0] solo_d;
    always @(*) begin
        case (solo_i)
            4'd1: solo_d = -11'sd1;
            4'd2: solo_d = 11'sd0;
            4'd3: solo_d = 11'sd1;
            4'd4: solo_d = -11'sd3;
            4'd5: solo_d = -11'sd2;
            4'd6: solo_d = 11'sd2;
            4'd7: solo_d = 11'sd3;
            default: solo_d = 11'sd0;
        endcase
    end
    wire [10:0] solo_addr_raw = {2'b00, solo_pos} + solo_d;
    wire [8:0]  solo_addr = (solo_addr_raw[10] || solo_addr_raw > 11'd511)
                            ? 9'd0 : solo_addr_raw[8:0];

    wire signed [23:0] thr = acf0 >>> 3;      // 峰要超过能量的 12.5%

    // ---------------------------------------------------------------- 输入
    always @(posedge clk) begin
        if (!rst_n) begin
            wp <= 11'd0;
            dout_valid <= 1'b0;
        end else begin
            dout_valid <= 1'b0;
            if (din_valid) begin
                // buf 的写挪到下面 A 口的 always 块里（一个块只写一个口）。
                // 上电后前 1024 点还没写满，此时开窗会读到"半新半旧"的环绕数据，
                // 那一帧的合成结果是纯噪声，会污染 acc。写满一轮之前不分析。
                // warm 的赋值放在"帧请求"那个 always 块里 —— 同一个 reg 被两个
                // always 块驱动在仿真里只是"后写者赢"，综合时直接报 multi-driven
                // （Synth 8-6859），而且清零会被吃掉。
                wp <= (wp == 11'd2047) ? 11'd0 : (wp + 11'd1);
                dout_valid <= 1'b1;
            end
        end
    end

    // buf 的 A 口：写输入 + 给输出口读。地址 wp 和 out_idx 差 1280，
    // 同一拍读写不同地址，真双口 BRAM 完全支持。
    always @(posedge clk) begin
        if (din_valid) buf_l[wp] <= din_l;
        buf_l_o <= buf_l[out_idx];
    end
    always @(posedge clk) begin
        if (din_valid) buf_r[wp] <= din_r;
        buf_r_o <= buf_r[out_idx];
    end
    // buf 的 B 口：给分析装载读（同步读）
    always @(posedge clk) begin
        bl_s <= buf_l[buf_rd_addr];
        br_s <= buf_r[buf_rd_addr];
    end

    // ---------------------------------------------------------------- 输出
    // 取 DELAY 之前的样本，扣掉"对面的那个人"。acc 取完即清 ——
    // DELAY 保证之后不会再有帧写这个位置。
    //
    // buf 和 acc 这边全部走**同步读**（buf_l_o / acc_m_o / ...）。out_idx 每
    // 2048 个样本才变一次，而 A/B 口每个时钟都在读它，所以取数那拍早就稳了。
    always @(posedge clk) begin
        if (!rst_n) begin
            dout_l <= 16'sd0;
            dout_r <= 16'sd0;
        end else if (din_valid) begin
            if (!en) begin
                dout_l <= din_l;
                dout_r <= din_r;
            end else begin
                // 左 = 人声（两块梳子的并集），右 = mid - 低频保护后的那份
                dout_l <= sat16(acc_m_o >>> OLA_SHIFT);
                dout_r <= sat16(val_mid - (acc_f_o >>> OLA_SHIFT));
            end
        end
    end

    // acc 的 B 口：读 + 清零。read-first，所以清零这一拍读到的正是清零**前**
    // 的累加值。一个 always 块只写一个口 —— 这是 BRAM 推断的硬规矩。
    always @(posedge clk) begin
        if (din_valid) acc_m[out_idx] <= 24'sd0;
        acc_m_o <= acc_m[out_idx];
    end
    always @(posedge clk) begin
        if (din_valid) acc_f[out_idx] <= 24'sd0;
        acc_f_o <= acc_f[out_idx];
    end

    // acc 的 A 口：合成累加，读 -> 加 -> 写回（读是同步的，所以要两段）
    wire acc_we_m = (state == S_RDVM) && (rph == 3'd4);
    wire acc_we_f = (state == S_RDVF) && (rph == 3'd4);
    always @(posedge clk) begin
        if (acc_we_m) acc_m[win_idx] <= sat24(acc_m_a + (o_ola >>> 15));
        acc_m_a <= acc_m[win_idx];
    end
    always @(posedge clk) begin
        if (acc_we_f) acc_f[win_idx] <= sat24(acc_f_a + (o_ola >>> 15));
        acc_f_a <= acc_f[win_idx];
    end

    // -------------------------------------------------- 谱 / 幅度 / 自相关
    // 这三块原来都是异步读，Vivado 只能推成 LUTRAM（1024×24 的自相关一块就
    // 384 个 LUT），加上 buf 一共 4800 个 LUT 被吃掉，35T 直接超 100%。
    // 改成"一个 always 块、一写一读"的简单双口之后就都能进 BRAM 了。
    //
    // 代价是每个读点要多等一拍，对应状态机里加一段（S_MASK / S_LDPW /
    // S_SOLO / S_PKM* 都改成了两段）。一帧多约 4000 拍，占预算不到 1%。
    wire [8:0] sp_raddr = (state == S_MASK) ? idx[8:0]
                        : (state == S_SOLO) ? solo_addr
                        : mirror_idx;      // S_LDPW / S_LDVM / S_LDVF

    wire sp_we_load = (state == S_RDMID) && (rph == 3'd3);
    wire sp_we_mask = (state == S_MASK)  && (dstep == 5'd17);
    wire sd_we      = (state == S_RDSD)  && (rph == 3'd3);
    wire acf_we     = (state == S_RDACF) && (rph == 3'd3);

    always @(posedge clk) begin
        if (sp_we_load)      sp_re[idx] <= f_rre;
        else if (sp_we_mask) sp_re[idx] <= sat24((v_mul_re + 41'sd16384) >>> 15);
        sp_re_r <= sp_re[sp_raddr];
    end
    always @(posedge clk) begin
        if (sp_we_load)      sp_im[idx] <= f_rim;
        else if (sp_we_mask) sp_im[idx] <= sat24((v_mul_im + 41'sd16384) >>> 15);
        sp_im_r <= sp_im[sp_raddr];
    end
    always @(posedge clk) begin
        if (sd_we) sd_mag[idx] <= amax_beta(f_rre, f_rim);
        sd_mag_r <= sd_mag[sp_raddr];
    end
    // acf 的读地址就是扫描指针 scan_l
    always @(posedge clk) begin
        if (acf_we) acf[idx] <= f_rre;
        acf_r <= acf[scan_l];
    end

    // ---------------------------------------------------------------- 帧请求
    always @(posedge clk) begin
        if (!rst_n) begin
            frame_req <= 1'b0;
            win_start <= 11'd0;
            warm      <= 1'b0;
        end else begin
            frame_req <= 1'b0;
            // 缓冲区写满一轮（2048 个槽里过了 1024 个）之后才允许开分析。
            // 放在这里而不是输入块，是为了让 warm 只有一个驱动源。
            if (din_valid && wp == 11'd1023) warm <= 1'b1;
            // 每 256 点一帧，分析窗 = 最新 1024 点。
            // 判据必须是 wp[7:0]==0（%256），写成 wp[8:0] 就变成 %512，
            // 每点只被两帧覆盖，Hann² 之和从 1.5 掉到 1，OLA 的 2/3 就补错了。
            if (en && warm && din_valid && wp[7:0] == 8'd0 && !frame_busy) begin
                win_start <= (wp - 11'd1024) & 11'd2047;
                frame_req <= 1'b1;
            end
        end
    end

    // ---------------------------------------------------------------- 主控
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_IDLE; idx <= 10'd0; ph <= 2'd0; rph <= 3'd0; dstep <= 5'd0;
            hidx <= 6'd0; frame_busy <= 1'b0; busy <= 1'b0;
            f_start <= 1'b0; f_inv <= 1'b0; f_wen <= 1'b0;
            f_waddr <= 10'd0; f_raddr <= 10'd0;
            f_wre <= 24'sd0; f_wim <= 24'sd0; buf_rd_addr <= 11'd0;
            mid_v <= 24'sd0; side_v <= 24'sd0; mag_m <= 24'd0;
            rem_r <= 25'sd0; c_q15 <= 16'd0; pw_r <= 24'sd0;
            acf0 <= 24'sd0; peak_m <= 24'sd0; peak_f <= 24'sd0;
            lag_m <= 10'd0; lag_f <= 10'd0; lag_m2 <= 10'd0;
            scan_l <= 10'd0; scan_best <= 24'sd0; scan_best_lag <= 10'd0;
            solo_pos <= 9'd0; solo_p <= 24'sd0; solo_bg <= 24'sd0; solo_i <= 4'd0;
            who_r <= 2'd0; lm_r <= 10'd0; lf_r <= 10'd0;
            who <= 2'd0; f0_m_lag <= 10'd0; f0_f_lag <= 10'd0;
        end else begin
            f_start <= 1'b0;
            f_wen   <= 1'b0;

            case (state)
                // ---------------------------------------------------- 空闲
                S_IDLE: begin
                    busy <= 1'b0;
                    if (frame_req) begin
                        frame_busy <= 1'b1; busy <= 1'b1;
                        idx <= 10'd0; ph <= 2'd3;      // 2'd3 = 装载的"等同步读"拍
                        buf_rd_addr <= win_start;
                        state <= S_LDMID;
                    end
                end

                // ---------------------------------------------------- mid 装载
                // bl_s / br_s 是 BRAM 的**同步**读，所以每点要走三段：
                //   ph==3  刚进状态，buf_rd_addr 上一拍才给出，空一拍等数据
                //   ph==0  取数算 mid，同时把地址推进到下一个
                //   ph==1  把加窗结果写进 FFT
                // 地址在 ph==0 的沿推进、同步读在 ph==1 的沿跟上，所以回到
                // ph==0 时数据正好是新的 —— 稳态每点仍是两拍，只多了一拍入口。
                S_LDMID: begin
                    if (ph == 2'd3) begin
                        ph <= 2'd0;
                    end else if (ph == 2'd1) begin
                        f_wen   <= 1'b1;
                        f_waddr <= idx;
                        f_wre   <= (m_win + 41'sd16384) >>> 15;
                        f_wim   <= 24'sd0;
                        f_inv   <= 1'b0;
                        ph <= 2'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0; f_start <= 1'b1; state <= S_FMID;
                        end else idx <= idx + 10'd1;
                    end else begin
                        mid_v <= ((bl_s + br_s) >>> 1) << 1;
                        buf_rd_addr <= (buf_rd_addr == 11'd2047) ? 11'd0
                                                                 : (buf_rd_addr + 11'd1);
                        ph <= 2'd1;
                    end
                end
                S_FMID: if (f_done) begin idx <= 10'd0; rph <= 3'd0; state <= S_RDMID; end

                S_RDMID: begin
                    if (rph == 3'd0) begin
                        f_raddr <= idx; rph <= 3'd1;
                    end else if (rph == 3'd1) begin
                        rph <= 3'd2;
                    end else if (rph == 3'd2) begin
                        rph <= 3'd3;                 // 见下面"读延迟"注释
                    end else begin
                        // 写 sp 交给上面 sp_re / sp_im 的 always 块（一个块一个口）
                        rph <= 3'd0;
                        if (idx == NBIN - 1) begin
                            idx <= 10'd0; buf_rd_addr <= win_start;
                            ph <= 2'd3; state <= S_LDSD;
                        end else idx <= idx + 10'd1;
                    end
                end

                // ---------------------------------------------------- side 装载
                // 同 S_LDMID，走 3（入口）/ 0（取数）/ 1（写）三段
                S_LDSD: begin
                    if (ph == 2'd3) begin
                        ph <= 2'd0;
                    end else if (ph == 2'd1) begin
                        f_wen   <= 1'b1;
                        f_waddr <= idx;
                        f_wre   <= (s_win + 41'sd16384) >>> 15;
                        f_wim   <= 24'sd0;
                        f_inv   <= 1'b0;
                        ph <= 2'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0; f_start <= 1'b1; state <= S_FSD;
                        end else idx <= idx + 10'd1;
                    end else begin
                        side_v <= ((bl_s - br_s) >>> 1) << 1;
                        buf_rd_addr <= (buf_rd_addr == 11'd2047) ? 11'd0
                                                                 : (buf_rd_addr + 11'd1);
                        ph <= 2'd1;
                    end
                end
                S_FSD: if (f_done) begin idx <= 10'd0; rph <= 3'd0; state <= S_RDSD; end

                S_RDSD: begin
                    if (rph == 3'd0) begin
                        f_raddr <= idx; rph <= 3'd1;
                    end else if (rph == 3'd1) begin
                        rph <= 3'd2;
                    end else if (rph == 3'd2) begin
                        rph <= 3'd3;                 // 见下面"读延迟"注释
                    end else begin
                        // 写 sd_mag 交给上面 sd_mag 的 always 块
                        rph <= 3'd0;
                        if (idx == NBIN - 1) begin
                            idx <= 10'd0; dstep <= 5'd0; state <= S_MASK;
                        end else idx <= idx + 10'd1;
                    end
                end

                // ---------------------------------------------------- 掩蔽
                // c = (|mid|-|side|)/|mid|，15 步移位减；V = c * mid
                // 走 18 步：dstep 0 等同步读 / 1 取幅度 / 2..16 做 15 步除法 /
                // 17 写回（写本身在 sp_re / sp_im 的 always 块里）。
                S_MASK: begin
                    if (dstep == 5'd0) begin
                        dstep <= 5'd1;               // 等 sp_*_r / sd_mag_r 就位
                    end else if (dstep == 5'd1) begin
                        mag_m <= amax_beta(sp_re_r, sp_im_r);
                        rem_r <= (amax_beta(sp_re_r, sp_im_r) >= sd_mag_r)
                                 ? {1'b0, amax_beta(sp_re_r, sp_im_r)
                                          - sd_mag_r} : 25'sd0;
                        c_q15 <= 16'd0;
                        dstep <= 5'd2;
                    end else if (dstep <= 5'd16) begin
                        // 15 步：16 步会得到 Q16，num==den 时商直接翻倍
                        if ((rem_r << 1) >= {1'b0, mag_m}) begin
                            rem_r <= (rem_r << 1) - {1'b0, mag_m};
                            c_q15 <= c_q15 | (16'd1 << (5'd16 - dstep));
                        end else begin
                            rem_r <= rem_r << 1;
                        end
                        dstep <= dstep + 5'd1;
                    end else begin
                        dstep <= 5'd0;
                        if (idx == NBIN - 1) begin
                            idx <= 10'd0; ph <= 2'd0; state <= S_LDPW;
                        end else idx <= idx + 10'd1;
                    end
                end

                // ---------------------------------------------------- |V|^2 实偶谱
                // 三段：ph=0 等 sp 的同步读 / ph=1 算 |V|^2 / ph=2 写给 FFT
                S_LDPW: begin
                    if (ph == 2'd0) begin
                        ph <= 2'd1;
                    end else if (ph == 2'd1) begin
                        pw_r <= idx_nyq ? 24'sd0 : $signed(pw_at_r(sp_re_r, sp_im_r));
                        ph <= 2'd2;
                    end else begin
                        f_wen   <= 1'b1;
                        f_waddr <= idx;
                        f_wre   <= pw_r;
                        f_wim   <= 24'sd0;
                        f_inv   <= 1'b1;
                        ph <= 2'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0; f_start <= 1'b1; state <= S_FACF;
                        end else idx <= idx + 10'd1;
                    end
                end
                S_FACF: if (f_done) begin idx <= 10'd0; rph <= 3'd0; state <= S_RDACF; end

                S_RDACF: begin
                    if (rph == 3'd0) begin
                        f_raddr <= idx; rph <= 3'd1;
                    end else if (rph == 3'd1) begin
                        rph <= 3'd2;
                    end else if (rph == 3'd2) begin
                        rph <= 3'd3;                 // 见下面"读延迟"注释
                    end else begin
                        // 写 acf 交给上面 acf 的 always 块
                        if (idx == 10'd0) acf0 <= f_rre;
                        rph <= 3'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0; scan_l <= LAG_M_LO;
                            scan_best <= 24'sd0; scan_best_lag <= 10'd0;
                            ph <= 2'd0; state <= S_PKM;
                        end else idx <= idx + 10'd1;
                    end
                end

                // ---------------------------------------------------- 找基频
                // acf 进了 BRAM，读是同步的，所以每个 lag 要走两段：
                //   ph=0 给地址（scan_l 已经在 acf 的读口上）等一拍
                //   ph=1 用 acf_r 比较，然后推进 scan_l
                S_PKM: begin
                    if (ph == 2'd0) begin
                        ph <= 2'd1;
                    end else begin
                        if (acf_r > scan_best) begin
                            scan_best <= acf_r;
                            scan_best_lag <= scan_l;
                        end
                        if (scan_l == LAG_M_HI) begin
                            peak_m <= scan_best; lag_m <= scan_best_lag;
                            scan_l <= LAG_F_LO; scan_best <= 24'sd0;
                            scan_best_lag <= 10'd0; ph <= 2'd0; state <= S_PKF;
                        end else begin
                            scan_l <= scan_l + 10'd1;
                            ph <= 2'd0;
                        end
                    end
                end

                S_PKF: begin
                    if (ph == 2'd0) begin
                        ph <= 2'd1;
                    end else begin
                        if (acf_r > scan_best) begin
                            scan_best <= acf_r;
                            scan_best_lag <= scan_l;
                        end
                        if (scan_l == LAG_F_HI) begin
                            peak_f <= scan_best; lag_f <= scan_best_lag;
                            if ((peak_m > thr) && (lag_m > 0) &&
                                (scan_best > thr) && (scan_best_lag > 0)) begin
                                // 两边都有峰：得先排除倍频鬼影再下结论
                                scan_l <= LAG_M_LO; scan_best <= 24'sd0;
                                scan_best_lag <= 10'd0; ph <= 2'd0; state <= S_PKM2;
                            end else begin
                                idx <= 10'd0; state <= S_SIMPLE;
                            end
                        end else begin
                            scan_l <= scan_l + 10'd1;
                            ph <= 2'd0;
                        end
                    end
                end

                // 只有一边有峰 —— 直接判
                S_SIMPLE: begin
                    if (peak_m > thr && lag_m > 0) begin
                        who_r <= 2'd1; lm_r <= lag_m; lf_r <= 10'd0;
                    end else if (peak_f > thr && lag_f > 0) begin
                        who_r <= 2'd2; lm_r <= 10'd0; lf_r <= lag_f;
                    end else begin
                        who_r <= 2'd0; lm_r <= 10'd0; lf_r <= 10'd0;
                    end
                    idx <= 10'd0; state <= S_CLRC;
                end

                // 挖掉 lag_f 的所有整数倍之后重扫男区（同样两段一个 lag）
                S_PKM2: begin
                    if (ph == 2'd0) begin
                        ph <= 2'd1;
                    end else begin
                        if (!is_ghost(scan_l, lag_f) && (acf_r > scan_best)) begin
                            scan_best <= acf_r;
                            scan_best_lag <= scan_l;
                        end
                        if (scan_l == LAG_M_HI) begin
                            lag_m2 <= scan_best_lag;
                            if (scan_best > thr && scan_best_lag > 0) begin
                                who_r <= 2'd3; lm_r <= scan_best_lag; lf_r <= lag_f;
                                idx <= 10'd0; state <= S_CLRC;
                            end else begin
                                // 差八度时挖鬼影会把男声自己也挖掉，
                                // 只能改看"男声基波位置有没有独立能量"
                                solo_i <= 4'd0; solo_p <= 24'sd0; solo_bg <= 24'sd0;
                                state <= S_SOLO;
                            end
                        end else begin
                            scan_l <= scan_l + 10'd1;
                            ph <= 2'd0;
                        end
                    end
                end

                // ---------------------------------------------------- 解差八度
                // 236 Hz 的女声在 118 Hz 处一点能量都没有，
                // 所以 118 Hz 立着峰就只能是独立男声。
                // 每个取样点两段：ph=0 等 sp 的同步读，ph=1 比较 / 累加
                S_SOLO: begin
                    if (solo_i == 4'd0) begin
                        solo_pos <= (rc_q8 + 24'd128) >>> 8;
                        solo_i <= 4'd1;
                        ph <= 2'd0;
                    end else if (solo_i <= 4'd3) begin
                        if (ph == 2'd0) begin
                            ph <= 2'd1;
                        end else begin
                            if (sp_pw > solo_p) solo_p <= sp_pw;
                            solo_i <= solo_i + 4'd1;
                            ph <= 2'd0;
                        end
                    end else if (solo_i <= 4'd7) begin
                        if (ph == 2'd0) begin
                            ph <= 2'd1;
                        end else begin
                            solo_bg <= solo_bg + sp_pw;
                            solo_i <= solo_i + 4'd1;
                            ph <= 2'd0;
                        end
                    end else begin
                        if ((solo_p > ((solo_bg >>> 2) * 3 + 24'd4)) &&
                            (solo_p > 24'd16)) begin
                            who_r <= 2'd3; lm_r <= lag_m; lf_r <= lag_f;
                        end else begin
                            who_r <= 2'd2; lm_r <= 10'd0; lf_r <= lag_f;
                        end
                        idx <= 10'd0; state <= S_CLRC;
                    end
                end

                // ---------------------------------------------------- 谐波梳
                S_CLRC: begin
                    cmb_m[idx] <= 2'd0;
                    cmb_f[idx] <= 2'd0;
                    if (idx == NBIN - 1) begin
                        idx <= 10'd0; hidx <= 6'd1;
                        state <= (who_r == 2'd2) ? S_CMBF : S_CMBM;
                    end else idx <= idx + 10'd1;
                end

                S_CMBM: begin
                    if ((lm_r > 0) && (combo_pos < NBIN)) begin
                        cmb_m[combo_pos] <= 2'd2;
                        if (combo_pos > 0 && cmb_m[combo_pos - 10'd1] == 2'd0)
                            cmb_m[combo_pos - 10'd1] <= 2'd1;
                        if (combo_pos < NBIN - 1 && cmb_m[combo_pos + 10'd1] == 2'd0)
                            cmb_m[combo_pos + 10'd1] <= 2'd1;
                    end
                    if ((hidx == HARM_MAX) || (combo_pos >= NBIN)) begin
                        hidx <= 6'd1;
                        state <= (who_r == 2'd1) ? S_LDVM : S_CMBF;
                    end else hidx <= hidx + 6'd1;
                end

                S_CMBF: begin
                    if ((lf_r > 0) && (combo_pos < NBIN)) begin
                        cmb_f[combo_pos] <= 2'd2;
                        if (combo_pos > 0 && cmb_f[combo_pos - 10'd1] == 2'd0)
                            cmb_f[combo_pos - 10'd1] <= 2'd1;
                        if (combo_pos < NBIN - 1 && cmb_f[combo_pos + 10'd1] == 2'd0)
                            cmb_f[combo_pos + 10'd1] <= 2'd1;
                    end
                    if ((hidx == HARM_MAX) || (combo_pos >= NBIN)) begin
                        idx <= 10'd0; ph <= 2'd0; state <= S_LDVM;
                    end else hidx <= hidx + 6'd1;
                end

                // ---------------------------------------------- 人声合成（左声道）
                S_LDVM: begin
                    if (ph == 2'd0) begin
                        ph <= 2'd1;
                    end else begin
                        f_wen   <= 1'b1;
                        f_waddr <= idx;
                        f_wre   <= idx_nyq ? 24'sd0
                                 : sat24((mk_re + 41'sd16384) >>> 15);
                        f_wim   <= idx_nyq ? 24'sd0
                                 : sat24(idx[9]
                                    ? -((mk_im + 41'sd16384) >>> 15)
                                    : ((mk_im + 41'sd16384) >>> 15));
                        f_inv   <= 1'b1;
                        ph <= 2'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0; f_start <= 1'b1; state <= S_FIM;
                        end else idx <= idx + 10'd1;
                    end
                end
                S_FIM: if (f_done) begin
                    idx <= 10'd0; rph <= 3'd0; state <= S_RDVM;
                end

                S_RDVM: begin
                    if (rph == 3'd0) begin
                        f_raddr <= idx; rph <= 3'd1;
                    end else if (rph == 3'd1) begin
                        rph <= 3'd2;
                    end else if (rph == 3'd2) begin
                        rph <= 3'd3;                 // 见下面"读延迟"注释
                    end else if (rph == 3'd3) begin
                        // 这一拍 f_rre 已经稳定；沿上顺带把 acc_m[win_idx]
                        // 打进 acc_m_a（BRAM 同步读），下一拍再写回。
                        rph <= 3'd4;
                    end else begin
                        // 真正的写发生在上面 acc 的 A 口那个 always 块里
                        rph <= 3'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0; ph <= 2'd0; state <= S_LDVF;
                        end else idx <= idx + 10'd1;
                    end
                end

                // -------------------------------------- 扣伴奏用的人声合成（右声道）
                S_LDVF: begin
                    if (ph == 2'd0) begin
                        ph <= 2'd1;
                    end else begin
                        f_wen   <= 1'b1;
                        f_waddr <= idx;
                        f_wre   <= idx_nyq ? 24'sd0
                                 : sat24((kf_re + 41'sd16384) >>> 15);
                        f_wim   <= idx_nyq ? 24'sd0
                                 : sat24(idx[9]
                                    ? -((kf_im + 41'sd16384) >>> 15)
                                    : ((kf_im + 41'sd16384) >>> 15));
                        f_inv   <= 1'b1;
                        ph <= 2'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0; f_start <= 1'b1; state <= S_FIF;
                        end else idx <= idx + 10'd1;
                    end
                end
                S_FIF: if (f_done) begin
                    idx <= 10'd0; rph <= 3'd0; state <= S_RDVF;
                end

                S_RDVF: begin
                    if (rph == 3'd0) begin
                        f_raddr <= idx; rph <= 3'd1;
                    end else if (rph == 3'd1) begin
                        rph <= 3'd2;
                    end else if (rph == 3'd2) begin
                        rph <= 3'd3;                 // 见下面"读延迟"注释
                    end else if (rph == 3'd3) begin
                        rph <= 3'd4;                 // 等 acc_f 的同步读
                    end else begin
                        // 真正的写发生在上面 acc 的 A 口那个 always 块里
                        rph <= 3'd0;
                        if (idx == 10'd1023) begin
                            idx <= 10'd0;
                            who <= who_r; f0_m_lag <= lm_r; f0_f_lag <= lf_r;
                            frame_busy <= 1'b0; busy <= 1'b0;
                            state <= S_IDLE;
                        end else idx <= idx + 10'd1;
                    end
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
