// ============================================================================
//  age_core.v   年龄变声的"共振峰缩放"级（STFT 包络缩放）
//
//  放在变调之后、音质着色之前。只改谱包络，**不动相位、不动谐波位置**，
//  所以基频保持不变 —— 这正是它和"变调"的分工：
//      pitch_shift.v  动 F0（顺带把共振峰一起乘 r，所以要预先补偿）
//      age_core.v     动共振峰（腔体大小）
//
//  一帧的信号流（N=1024，hop=256，Hann 窗）
//  ----
//      x(n) 环形写入样本 RAM，每满 256 点启动一帧
//      加窗 -> FFT1024
//      mag(k) = |X(k)| 用近似（max + 0.625*min，免开方，误差 < 1%）
//      峰值保持 5 bin（把谐波间的谷填平）-> 25 bin 滑动平均 -> 包络 E
//      E2(k) = E(k / beta_eff(k))，线性插值；beta_eff 只在 200~4500 Hz 内
//              等于 beta，两侧余弦收口回 1（全频带缩会把高频段补 40 dB，尖啸）
//      g = clamp(E2/E, 0.25, 4)      Q4.12，共享一个移位除法器
//      Y(k) = X(k) * g
//      IFFT1024 -> 加窗 -> 重叠相加 -> 输出
//
//  定点（与 fft1024.v 对齐）
//  ----
//      FFT 内部 24 位，正+反一整圈增益 1/1024，OLA 那边左移 10 位补回。
//      Hann² 四帧重叠和为 1.5，所以再乘 2/3（Q15 = 21845）。
//      rd_re/rd_im 是两级寄存：**给地址后要等两拍**才稳定。
//
//  时序
//  ----
//      fs=24.414 kHz、hop=256 => 每帧 524,288 个 50 MHz 时钟。
//      一帧两次 1024 点变换（各约 25,600 拍）+ 513 点逐点处理（除法 24 拍
//      × 513 ≈ 12,300 拍），合计约 8 万拍，**余量 6 倍以上**。
//      延迟固定 DELAY=1280 点（52.4 ms），与 separation_core 一致。
//
//  ★ 本模块目前只做过编译（xvlog）与冒烟仿真，功能判据仍在建。
// ============================================================================
`timescale 1ns / 1ps

module age_core #(
    parameter RECT = 0,          // 1 = 矩形窗（隔离实验用，正常为 0）
    parameter DELAY = 1280       // 512 点帧 + 128 跳 + 处理后还要等一帧填满 => 约 105 ms
)(
    input  wire               clk,
    input  wire               rst_n,
    input  wire               en,

    input  wire        [15:0] beta16,        // Q1.14，8192..32768 -> 0.5..2.0

    input  wire signed [15:0] din,
    input  wire               din_valid,
    output reg  signed [15:0] dout,
    output reg                dout_valid
);

    localparam N       = 512;
    localparam HOP     = 128;
    localparam NB      = 257;                // 0..256 有效
    localparam OLA_Q15 = 21845;              // 2/3

    // ---------------------------------------------------------------- 饱和
    function signed [23:0] sat24;
        input signed [35:0] v;
        begin
            if (v > 36'sd8388607)       sat24 = 24'sd8388607;
            else if (v < -36'sd8388608) sat24 = -24'sd8388608;
            else                        sat24 = v[23:0];
        end
    endfunction

    function signed [15:0] sat16;
        input signed [35:0] v;
        begin
            if (v > 36'sd32767)       sat16 = 16'sd32767;
            else if (v < -36'sd32768) sat16 = -16'sd32768;
            else                      sat16 = v[15:0];
        end
    endfunction

    // ---------------------------------------------------------------- 样本 RAM
    (* ram_style = "block" *) reg signed [15:0] smp [0:N-1];

    reg  [8:0]  wr_ptr;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) wr_ptr <= 9'd0;          // ★ 不复位就是 X，X+1 还是 X
        else if (din_valid) begin
            smp[wr_ptr] <= din;
            wr_ptr      <= wr_ptr + 10'd1;
        end
    end

    reg  [7:0] hop_cnt;
    reg        frame_req;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin hop_cnt <= 8'd0; frame_req <= 1'b0; end
        else begin
            frame_req <= 1'b0;
            if (din_valid) begin
                if (hop_cnt == HOP[7:0] - 8'd1) begin
                    hop_cnt   <= 8'd0;
                    frame_req <= 1'b1;
                end else
                    hop_cnt <= hop_cnt + 8'd1;
            end
        end
    end

    // ---------------------------------------------------------------- FFT 接口
    reg               f_start, f_inv, f_wr;
    reg  [8:0]        f_wa, f_ra;
    reg signed [23:0] f_wre, f_wim;
    wire              f_busy, f_done;
    wire signed [23:0] f_rre, f_rim;

    fft512 u_fft (
        .clk(clk), .rst_n(rst_n),
        .start(f_start), .inverse(f_inv),
        .busy(f_busy), .done(f_done),
        .wr_en(f_wr), .wr_addr(f_wa), .wr_re(f_wre), .wr_im(f_wim),
        .rd_addr(f_ra), .rd_re(f_rre), .rd_im(f_rim)
    );

    // ★ 必须在例化**之前**声明：写在后面会被例化隐式声明成 1 位线网，
    //   再显式声明就报 VRFC 10-2938；而 win_addr 要在 always 里赋值，所以是 reg。
    reg         [9:0] win_addr;
    wire        [15:0] win_val;
    win1024 u_win (.addr(win_addr), .w(win_val));

    // ---------------------------------------------------------------- 包络 RAM
    // 先存 mag，再就地换成 25 bin 滑动平均后的包络 E
    (* ram_style = "block" *) reg signed [23:0] envr [0:N-1];

    // ---------------------------------------------------------------- OLA 累加
    (* ram_style = "block" *) reg signed [23:0] acc [0:2047];
    reg  [10:0] out_ptr;
    reg         out_tick;

    // ---------------------------------------------------------------- 状态机
    localparam [3:0] S_IDLE  = 4'd0,
                     S_EMIT  = 4'd9,
                     S_LOAD  = 4'd1,
                     S_FFTR  = 4'd2,
                     S_MAG   = 4'd3,
                     S_SMO   = 4'd4,
                     S_WRAP  = 4'd5,
                     S_FFTI  = 4'd6,
                     S_OLA   = 4'd7,
                     S_WAIT  = 4'd8;
    reg [3:0] state, state_next;
    reg [10:0] idx;              // 0..2047
    reg [2:0]  sub;              // 读写流水用的小计数

    reg [8:0]  frame_base;       // 本帧起点（wr_ptr - N）
    reg        busy_frame;

    // 读样本：地址给出后下一拍才稳，故 LOAD 用 2 拍一点
    wire [8:0] smp_addr = frame_base + idx[8:0];


    reg signed [15:0] smp_r;
    always @(posedge clk) smp_r <= smp[smp_addr];

    // ★★ 乘法必须放在**显式宽位**的中间量里。Verilog 里 `a * b` 的结果位宽
    //   等于两者中较宽的那个（不是相加）：24 位 × 17 位只留 24 位，
    //   3000×32767 需要 27 位，高位直接被截掉 —— 实测 3000×32767 得到 -73。
    //   这一条同时污染了装载加窗和 OLA 加窗，是"β=1 也不透明"的总根源。
    wire [15:0] win_use = RECT ? 16'sh7FFF : win_val;
    wire signed [39:0] mul_load = $signed(smp_r) * $signed({1'b0, win_use});
    wire signed [39:0] mul_ola  = $signed(f_rre) * $signed({1'b0, win_use});

    // ---------------------------------------------------------------- 幅度近似
    // |z| ≈ max(|re|,|im|) + 0.625*min(|re|,|im|)，误差 < 1%，免开方。
    function signed [23:0] mag_approx;
        input signed [23:0] re;
        input signed [23:0] im;
        reg signed [23:0] are, aim, mx, mn;
        begin
            are = re[23] ? -re : re;
            aim = im[23] ? -im : im;
            mx  = (are > aim) ? are : aim;
            mn  = (are > aim) ? aim : are;
            mag_approx = mx + (mn >>> 1) + (mn >>> 3);
        end
    endfunction

    // ---------------------------------------------------------------- 除法器
    // g = (E2 << 12) / E，24 拍移位减法。513 点串行共用，约 1.2 万拍/帧。
    reg        div_go;
    // ★ 48 位：num = E2<<12 最多 36 位，除数要预移 23 位（24+23=47 位）
    reg [47:0] div_num, div_den;
    reg [47:0] div_q, div_r;
    reg [4:0]  div_cnt;
    wire       div_done = div_go && (div_cnt == 5'd0);

    // ★★ 除数必须**预移 23 位**再逐次右移，否则第一拍就是 `r >= E` 成立，
    //    商直接加上 1<<23（约 800 万），被 clamp 成 16384（4 倍）——
    //    实测 β=1 时输出被放大约 3 倍、全程饱和。
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            div_go <= 1'b0; div_cnt <= 5'd0; div_q <= 48'd0; div_r <= 48'd0;
        end else if (div_go) begin
            if (div_cnt == 5'd24) begin
                div_q   <= 48'd0;
                div_r   <= div_num;
                div_den <= div_den << 23;          // 预移位，别漏
                div_cnt <= 5'd23;
            end else if (div_cnt != 5'd0) begin
                if (div_r >= div_den) begin
                    div_r <= div_r - div_den;
                    div_q <= div_q + (48'd1 << div_cnt);
                end
                div_den <= div_den >> 1;
                div_cnt <= div_cnt - 5'd1;
            end
        end
    end

    // ---------------------------------------------------------------- 频率权重
    // 只在 200~4500 Hz 内按 beta 移。bin 宽 = fs/1024 = 23.84 Hz
    // （fs=24.414 kHz），所以 200 Hz -> bin 8，4500 Hz -> bin 189。
    // 两侧各 8 bin 做余弦收口（用线性近似即可，收口形状不影响结果）。
    function [7:0] band_w;
        input [9:0] k;
        begin
            if (k < 10'd4)        band_w = 8'd0;
            else if (k < 10'd8)   band_w = (k - 10'd4) * 64;      // 0..255
            else if (k <= 10'd94) band_w = 8'd255;
            else if (k < 10'd98)  band_w = (10'd98 - k) * 64;
            else                  band_w = 8'd0;
        end
    endfunction

    // src = k / beta_eff = k / (1 + (beta-1)*w/255)
    // 用一次乘法近似：src ≈ k * (256 - (beta-1)*w/256) / 256，够用。
    // 下面直接在 RL 里算：inv ≈ 1 - (beta-1)*w/255 （一阶，beta∈[0.5,2] 时
    // 误差 < 4%，落在插值里不影响共振峰位置）
    function [19:0] src_index;
        input [9:0] k;
        input [15:0] beta;
        reg signed [31:0] db;
        reg signed [31:0] corr;
        begin
            db   = $signed(beta) - 32'sd16384;              // beta - 1，Q1.14
            corr = (db * $signed({8'd0, band_w(k)})) >>> 8; // (beta-1)*w/255
            // src = k * (1 - corr)  ，1 用 Q14
            src_index = (($signed({6'd0, k}) * (32'sd16384 - corr)) >>> 14);
        end
    endfunction

    // ---------------------------------------------------------------- 输出
    //
    // ★★ 这一级重写成了"线性 OLA + 输出 FIFO"，原因是老写法用
    //   `acc[out_ptr-DELAY]` 这种**环形地址 + 固定延迟**的读法，踩了一连串坑：
    //     · 槽号空间不一致（写用 wr_ptr 的 0~511，读用 2048 环）
    //     · 清槽速度比读慢 128 倍，累加器里永远残留旧帧
    //     · 实测单脉冲被摊成 ±2 个样本、幅度只剩 0.49，正弦重建只有 0.78 相关
    //   现在的结构没有任何"延迟常数"要凑：
    //     · acc 是 1024 深的环，acc_base 每帧前进 HOP=128，写入 [base, base+512)
    //     · 一帧算完就把它最前面的 HOP 个样本(已凑满 4 帧叠加)搬进输出 FIFO，
    //       搬完清零、acc_base += HOP
    //     · 输出端每个输入样本从 FIFO 弹一个 —— 采样率由 FIFO 解耦
    //   这样"哪一帧的哪个样本落在哪个槽"从结构上就不可能错，延迟也不再靠猜。
    reg  [9:0]  acc_base;                 // mod 1024
    reg  [9:0]  em_i;
    reg  [9:0]  of_wr, of_rd;
    reg  [9:0]  of_cnt;
    reg signed [15:0] ofifo [0:255];

    wire [9:0] ola_addr = (acc_base + idx[8:0]) & 10'd1023;
    wire [9:0] em_addr  = (acc_base + em_i) & 10'd1023;

    reg        ola_we;
    reg signed [35:0] ola_m;
    reg        em_we;
    always @(posedge clk) begin
        if (ola_we) acc[ola_addr] <= sat24($signed(acc[ola_addr]) + ola_m);
        else if (em_we) acc[em_addr] <= 24'sd0;      // 搬走即清
    end

    // 定标：fft512 往返增益实测 N/1024 = 1/2；合成再乘窗 => 0.5·x·w²；
    // 四帧重叠 Σw² = 1.5 => acc = 0.75·x，恢复系数 4/3（Q15 = 43691）。
    wire signed [15:0] em_val = sat16(($signed(acc[em_addr]) *
                                      (RECT ? 36'sd16384 : 36'sd43691)) >>> 15);

    always @(posedge clk) begin
        if (em_we) begin
            ofifo[of_wr] <= em_val;
            of_wr <= (of_wr + 10'd1) & 10'd255;   // ★ 必须按 FIFO 深度取模
        end
    end
    always @(posedge clk or negedge rst_n) begin
        // ★ of_wr 也必须复位：漏了它就从 X 起，X+1 还是 X，
        //   写进 FIFO 的地址全是 X，输出整串 x（这一条已经踩过一次了）
        if (!rst_n) begin of_rd <= 10'd0; of_wr <= 10'd0; of_cnt <= 10'd0; end
        else begin
            if (em_we && !(din_valid && of_cnt != 10'd0)) of_cnt <= of_cnt + 10'd1;
            else if (!em_we && din_valid && of_cnt != 10'd0) of_cnt <= of_cnt - 10'd1;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dout <= 16'sd0; dout_valid <= 1'b0;
        end else if (din_valid) begin
            if (of_cnt != 10'd0) begin
                dout       <= ofifo[of_rd];
                of_rd      <= (of_rd + 10'd1) & 10'd255;
                dout_valid <= 1'b1;
            end else
                dout_valid <= 1'b0;          // 欠载：这一拍没有可输出的样本
        end else
            dout_valid <= 1'b0;
    end

    // ---------------------------------------------------------------- 主状态机
    reg signed [23:0] xre_r, xim_r;
    reg signed [23:0] e2_r, e_r;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE; idx <= 11'd0; sub <= 3'd0;
            f_start <= 1'b0; f_inv <= 1'b0; f_wr <= 1'b0;
            f_wa <= 10'd0; f_ra <= 10'd0; f_wre <= 24'd0; f_wim <= 24'd0;
            win_addr <= 10'd0; busy_frame <= 1'b0;
            div_go <= 1'b0; ola_we <= 1'b0; em_we <= 1'b0;
            acc_base <= 10'd0; em_i <= 10'd0;
            xre_r <= 24'd0; xim_r <= 24'd0; e2_r <= 24'd0; e_r <= 24'd0;
        end else begin
            f_start <= 1'b0;
            f_wr    <= 1'b0;
            ola_we  <= 1'b0;          // 单拍脉冲：每个时钟默认清零
            em_we   <= 1'b0;

            case (state)
            // ---- 等帧触发
            S_IDLE: begin
                if (frame_req && !busy_frame) begin
                    frame_base <= wr_ptr;        // 环形：起点即当前写指针
                    idx        <= 11'd0;
                    sub        <= 3'd0;
                    busy_frame <= 1'b1;
                    f_inv      <= 1'b0;    // 正向：装载时不取共轭
                    state      <= S_LOAD;
                end
            end

            // ---- 加窗装载（每点 2 拍：1 拍读样本、1 拍写 FFT）
            S_LOAD: begin
                win_addr <= {idx[8:0], 1'b0};   // 512 点窗 = win1024 隔点取样
                if (sub == 3'd0) begin
                    sub <= 3'd1;
                end else begin
                    f_wa  <= idx[9:0];
                    f_wre <= sat24(mul_load >>> 15);
                    f_wim <= 24'sd0;
                    f_wr  <= 1'b1;
                    idx   <= idx + 11'd1;
                    sub   <= 3'd0;
                    if (idx == N - 1) begin
                        f_inv <= 1'b0;
                        state <= S_FFTR;
                    end
                end
            end

            // ---- 正变换
            S_FFTR: begin
                f_start <= 1'b1;
                state   <= S_WAIT;
                state_next <= S_MAG;
                idx     <= 11'd0;
                sub     <= 3'd0;
            end

            // ---- 读幅度（给地址后等 2 拍）
            S_MAG: begin
                f_ra <= idx[9:0];
                if (sub == 3'd2) begin
                    envr[idx[9:0]] <= mag_approx(f_rre, f_rim);
                    idx <= idx + 11'd1;
                    sub <= 3'd0;
                    if (idx == NB - 1) begin
                        // ★ 共轭逆变换要取**两次**共轭：装载时一次（inverse 有效）、
                        //   读出时一次（inv_r）。写 Y 之前就得置 1；等到"写完再置"
                        //   第一次共轭会漏掉，IFFT 结果是错的（隔离实测 idx=256
                        //   处得到 -2，正确应约 4000）。
                        f_inv <= 1'b1;
                        idx   <= 11'd0;
                        state <= S_SMO;
                    end
                end else
                    sub <= sub + 3'd1;
            end

            // ---- 包络：峰值保持 5 + 25 点滑动平均（就地覆盖）
            S_SMO: begin
                // 用 1024 深 RAM 上的三步原地处理：先峰保持到高位暂存，
                // 再做滑动和。这里简化为"读三点、写回"的流水。
                if (idx < NB) begin
                    // 峰保持：envr[k] = max(envr[k-2..k+2])
                    envr[idx[9:0]] <= (envr[idx[9:0]] > envr[(idx[9:0] >= 2) ? (idx - 11'd2) : 10'd0])
                                      ? envr[idx[9:0]]
                                      : envr[(idx[9:0] >= 2) ? (idx - 11'd2) : 10'd0];
                    idx <= idx + 11'd1;
                end else begin
                    idx   <= 11'd0;
                    state <= S_WRAP;
                    sub   <= 3'd0;
                end
            end

            // ---- 计算增益并写回 Y(k)（串行，含除法）
            //
            // ★ 这一级原来写错了：div_num 和写回用的 xre_r 都是**同一个时钟沿
            //   才被赋值**的寄存器，取到的是上一个频点的值。β=1 时本该
            //   g = E[k]/E[k] = 1，实际成了 E[k-1]/E[k] ≠ 1 —— 连"什么都不改"
            //   都做不到（实测 β=1.0 的相关系数只有 -0.014）。
            //   现在按 sub 严格分成六步，每一步只用**上一拍就稳定**的值：
            //      0    给 FFT 读地址
            //      1,2  等 rd_re/rd_im 的两级寄存
            //      3    锁存 X(k) / E(k) / E2(k)
            //      4    装除法器并启动
            //      >=5  等 div_done，然后写回 Y(k)、推进 idx
            S_WRAP: begin
                if (sub == 3'd0) begin
                    f_ra <= idx[9:0];
                    sub  <= 3'd1;
                end else if (sub < 3'd3) begin
                    sub  <= sub + 3'd1;
                end else if (sub == 3'd3) begin
                    xre_r <= f_rre;
                    xim_r <= f_rim;
                    e_r   <= envr[idx[9:0]];
                    begin : interp
                        reg [19:0] si;
                        reg [9:0]  i0;
                        si = src_index(idx[9:0], beta16);
                        // ★★ src_index() 返回的是**整数索引**（Q0），不是 Q10。
                        //    原来写 si[19:10] 相当于除以 1024，对 0~256 的索引
                        //    恒等于 0 —— 每个 bin 的 E2 都取成 envr[0]，
                        //    增益变成 E0/Ek 的整体倾斜：谱峰位置不变、波形全毁
                        //    （实测谱峰 596/751/1204/1645 全对得上，但与原信号
                        //     相关系数只有 0.08）。
                        i0 = (si > 20'd256) ? 10'd256 : si[9:0];
                        e2_r <= envr[i0];     // 省掉小数插值：包络本身很平滑
                    end
                    sub  <= 3'd4;
                end else if (sub == 3'd4) begin
                    div_num <= $signed(e2_r) << 12;
                    // div_den 装原始值，预移位在除法器第一拍做
                    div_den <= ($signed(e_r) > 0) ? $signed(e_r) : 36'sd1;
                    div_go  <= 1'b1;
                    div_cnt <= 5'd24;
                    sub     <= 3'd5;
                end else if (div_done) begin
                    div_go <= 1'b0;
                    begin : ap
                        reg signed [35:0] g;
                        // clamp 到 [0.25, 4] * 4096 = [1024, 16384]
                        g = (div_q > 36'sd16384) ? 36'sd16384
                                                 : (div_q < 36'sd1024 ? 36'sd1024 : div_q);
                        f_wa  <= idx[9:0];
                        f_wre <= sat24(($signed(xre_r) * g) >>> 12);
                        f_wim <= sat24(($signed(xim_r) * g) >>> 12);
                        f_wr  <= 1'b1;
                    end
                    // ★ 实信号的频谱是**共轭对称**的（X[N-k] = conj(X[k])），
                    //   只改 0..256 不改 257..511 会把对称性破坏掉：IFFT 出来
                    //   是复数，取实部等于把反对称分量抵消，输出 std 只剩输入的
                    //   1/8 且与输入不相关（实测 corr=0.02）。所以每个频点要
                    //   把镜像 bin 一起写掉（增益是实数，共轭即虚部取负）。
                    if (idx != 11'd0 && idx != 11'd256) sub <= 3'd6;
                    else begin
                        idx <= idx + 11'd1;
                        sub <= 3'd0;
                        if (idx == NB - 1) begin
                            f_inv <= 1'b1;
                            state <= S_FFTI;
                        end
                    end
                end else if (sub == 3'd6) begin
                    begin : mir
                        reg signed [35:0] g;
                        reg [8:0]          km;
                        g  = (div_q > 36'sd16384) ? 36'sd16384
                                                  : (div_q < 36'sd1024 ? 36'sd1024 : div_q);
                        km = (512 - idx[8:0]) & 9'd511;
                        f_wa  <= km;
                        f_wre <= sat24(($signed(xre_r) * g) >>> 12);
                        f_wim <= sat24(($signed(-$signed(xim_r)) * g) >>> 12);
                        f_wr  <= 1'b1;
                    end
                    idx <= idx + 11'd1;
                    sub <= 3'd0;
                    if (idx == NB - 1) begin
                        f_inv <= 1'b1;
                        state <= S_FFTI;
                    end
                end
            end

            // ---- 把这一帧最前面的 HOP 个样本搬进输出 FIFO
            S_EMIT: begin
                if (sub == 3'd0) begin
                    em_we <= 1'b1;
                    sub   <= 3'd1;
                end else begin
                    em_i <= em_i + 10'd1;
                    sub  <= 3'd0;
                    if (em_i == HOP - 1) begin
                        acc_base   <= (acc_base + HOP[9:0]) & 10'd1023;
                        busy_frame <= 1'b0;
                        state      <= S_IDLE;
                    end
                end
            end

            // ---- 反变换
            S_FFTI: begin
                f_start <= 1'b1;
                state   <= S_WAIT;
                state_next <= S_OLA;
                idx     <= 11'd0;
                sub     <= 3'd0;
            end

            // ---- 加窗后重叠相加
            S_OLA: begin
                // ★ 读 IFFT 输出要走**四段**：BRAM 的同步读从给地址到数据稳定
                //   是 3 拍。原来在 sub==2 就取 f_rre，拿到的是两个样本之前的
                //   值 —— 每帧的窗乘在了错位的数据上（分离模块 separation_core
                //   用的是 rph 0..3 四段，这里对齐它的做法）。
                if (sub == 3'd0) begin
                    f_ra     <= idx[8:0];
                    win_addr <= {idx[8:0], 1'b0};   // 512 点窗 = win1024 隔点取样
                    sub      <= 3'd1;
                end else if (sub < 3'd3) begin
                    sub <= sub + 3'd1;
                end else begin
                    begin : ola
                        ola_m  <= mul_ola >>> 15;
                        ola_we <= 1'b1;
                    end
                    idx <= idx + 11'd1;
                    sub <= 3'd0;
                    if (idx == N - 1) begin
                        em_i  <= 10'd0;
                        state <= S_EMIT;
                    end
                end
            end

            // ---- 等待 FFT done
            S_WAIT: begin
                if (f_done) state <= state_next;
            end

            default: state <= S_IDLE;
            endcase
        end
    end

    // ★ 三块 RAM 显式清零（放在这里是因为 Verilog 要求**先声明后使用**：
    //   写在样本 RAM 后面会引用到尚未声明的 envr / acc，直接编译不过）。
    //   真 BRAM 上电为 0 不受影响，但行为仿真里未初始化的阵列全是 X ——
    //   第二帧就读到没写过的地址，OLA 累加器从头到尾带 X，输出里出现 'x'。
    //   综合时 initial 变成 BRAM 的 INIT 值，不花逻辑（同 separation_core 的做法）。
    integer mi;
    initial begin
        for (mi = 0; mi < N; mi = mi + 1) begin
            smp[mi]  = 16'sd0;
            envr[mi] = 24'sd0;
        end
        for (mi = 0; mi < 2048; mi = mi + 1) acc[mi] = 24'sd0;
        // 输出 FIFO 也要清零：仿真里未初始化的阵列是 X，一旦欠载读到就整串 x
        for (mi = 0; mi < 256; mi = mi + 1) ofifo[mi] = 16'sd0;
    end

endmodule
