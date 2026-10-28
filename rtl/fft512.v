// ============================================================================
//  fft512.v    512 点复数 FFT / IFFT（由 fft1024.v 移植，算法与时序完全相同）
//
//  为什么单独一份而不参数化 fft1024：fft1024 已在 separation_core 里标定并跑通，
//  改它要重跑那边的仿真。这里只做三处机械改动：
//      1024 -> 512（点数）、级数 10 -> 9、地址 10 位 -> 9 位
//  旋转因子复用同一张 512 项 ROM（fft_twiddle，addr 9 位）：
//      N=1024 时角度 = 2*pi*addr/1024；N=512 需要 2*pi*k/512，即 addr = 2k，
//      所以地址写成 jj << (stage+1)（原来写法是 jj << stage）。
//  缩放仍取末 5 级（stage>=4，原来是 stage>=5），总位移还是 1/32，
//  于是"分析+合成"整圈增益依然是 1/1024 —— 上层的左移 10 位不用改。
//
//  为什么可以这么"慢"
//  ----------------
//  音频速率下（本工程 fs = 24.414 kHz、帧跳 256 点 = 10.5 ms）一帧有
//  50 MHz × 10.5 ms ≈ 52 万个时钟，而一次 1024 点 FFT 只要约 1.6 万个。
//  **时间富余 30 倍**，所以完全不必并行：一个蝶形单元串行跑完，面积省到底。
//
//  算法：DIF（Gentleman-Sande），自然序输入 / 位倒序输出
//      第 s 级（s = 0..9），半组 half = 1024 >> (s+1)
//          x0' = x0 + x1
//          x1' = (x0 - x1) * exp(-j*2*pi*(j<<s)/1024)
//      旋转因子的地址就是 j<<s —— 一个移位，不需要乘法（见 fft_twiddle.v）。
//
//  为什么必须是"单 RAM 原址"（别改回乒乓）
//  --------------------------------------
//  DIF 蝶形读 a、b 两点，算完又写回 a、b —— 天生就是原址的。所以整块
//  数据只要一块 RAM：端口 A 管 a、端口 B 管 b，每个口各 1 读 1 写，
//  正好是 Xilinx 真双口 BRAM 的标准模板。
//
//  之前那版是"源 RAM + 目的 RAM 乒乓"，结果是：
//      源 RAM  读 addr0 + 读 addr1 + 写 wr_addr + 回拷写 = 4 个口
//      目的 RAM 读回拷 + 写 addr0 + 写 addr1          = 3 个口
//  BRAM36K 只有 2 个口，Vivado 直接把四块 RAM 全拆成寄存器
//  （Synth 8-4767 "multiple writes via different ports in same process"），
//  98 Kbit 吃掉 101,752 个触发器 —— 35T 一共才 41,600 个，根本装不下。
//  改成原址之后 FFT 只占 2 块 BRAM36K，连每级 1024 拍的回拷都省了。
//
//  BRAM 推断的三条硬规矩（改这个文件前先看这里）
//  ------------------------------------------
//  1. 每个 always 块只能写**一个**口（端口 A 一个块、端口 B 一个块）。
//     装载写和蝶形写都走端口 B，所以必须写在**同一个** always 里。
//  2. RAM 不能出现在复位分支里 —— 带复位的 RAM 会被拆成寄存器。
//     清零靠 initial（综合时变成 BRAM 的 INIT 值，不花逻辑）。
//  3. 读要无条件（read-first 模板）：`if (we) mem[a] <= d;  dout <= mem[a];`
//     写成 if/else 的 write-first 会让推断失败。
//
//  定点与缩放
//  ----------
//  24 位内部数据、Q15 旋转因子，后 5 级每级右移 1 位（共 1/32）。
//  输入幅度上界 A 时，第 s 级之后约为 2A，全程不溢出；代价是输出有效位
//  从 24 降到约 19 位。做的是"掩蔽比值"这类运算，16 位音频足够。
//
//  IFFT 用共轭法：IFFT(X) = conj(FFT(conj(X)))，正反向共用一个引擎。
//  于是"分析 → 掩蔽 → 合成"整圈增益恰好是 1/1024，重叠相加那边左移 10 位补回。
//
//  接口
//  ----
//      start 之前用 wr_en 把 1024 点写进去（自然序）
//      start 拉高一拍 → busy 变高 → done 变高一拍
//      done 之后从 rd_addr 读（内部做位倒序，读到的是自然序）
//      rd_re/rd_im 是两级寄存器输出：**给地址之后要等两拍**才稳定
// ============================================================================
`timescale 1ns / 1ps

module fft512 (
    input  wire                clk,
    input  wire                rst_n,

    input  wire                start,
    input  wire                inverse,       // 1 = IFFT
    output reg                 busy,
    output reg                 done,

    input  wire                wr_en,
    input  wire [8:0]          wr_addr,
    input  wire signed [23:0]  wr_re,
    input  wire signed [23:0]  wr_im,

    input  wire [8:0]          rd_addr,
    output reg  signed [23:0]  rd_re,
    output reg  signed [23:0]  rd_im
);

    localparam integer NB = 9;   // 512 = 2^9，9 级蝶形（1024 点时是 10）

    // ------------------------------------------------------------ 数据 RAM
    // 512 × 24 bit = 12 Kbit/块，各占一块 BRAM36K（36K 模式下 512 深
    // 最宽 36 位，24 位正好放得下）。实部虚部分开两块，读写互不干扰。
    (* ram_style = "block" *) reg signed [23:0] m_re [0:511];
    (* ram_style = "block" *) reg signed [23:0] m_im [0:511];

    // 仿真里 RAM 不写初值的话，上电后第一次变换会读进 x 并一路传播，
    // 第一帧的结果全是 x（第二次之后就正常了，很容易被当成"只是仿真问题"
    // 而放过去）。综合时 Vivado 会把 initial 直接做成 BRAM 初值，不花逻辑。
    integer m_i;
    initial begin
        for (m_i = 0; m_i < 512; m_i = m_i + 1) begin
            m_re[m_i] = 24'sd0;
            m_im[m_i] = 24'sd0;
        end
    end

    // ------------------------------------------------------------ 旋转因子
    wire signed [15:0] tw_re, tw_im;

    // ------------------------------------------------------------ 控制
    localparam [1:0] S_IDLE = 2'd0, S_BFLY = 2'd1, S_DONE = 2'd2;
    reg [1:0]  state;
    reg [3:0]  stage;
    reg [8:0]  base;
    reg [8:0]  jj;
    reg [1:0]  ph;                  // 0=给地址 1=取数 2=写回
    reg        inv_r;

    wire [10:0] half = 11'd512 >> (stage + 1);
    wire [10:0] blk  = {half[8:0], 1'b0};
    wire [8:0] bf0   = base + jj;
    wire [8:0] bf1   = bf0 + half[8:0];
    wire [8:0] tw_addr  = jj[7:0] << (stage + 1);
    wire [10:0] blk_end = base + blk;

    fft_twiddle u_tw (.addr(tw_addr), .tw_re(tw_re), .tw_im(tw_im));

    // 位倒序（10 位）：DIF 原址算完是位倒序存放的
    wire [8:0] rbr = {rd_addr[0], rd_addr[1], rd_addr[2], rd_addr[3], rd_addr[4],
                      rd_addr[5], rd_addr[6], rd_addr[7], rd_addr[8]};

    // ------------------------------------------------------------ 端口地址
    // A 口：忙时做蝶形的"下半"地址 a，空闲时给外部读结果（位倒序地址）
    // B 口：忙时做蝶形的"上半"地址 b，空闲时给外部装载输入
    // 外部只在 !busy 时读写，所以两个口在时间上完全复用，不冲突。
    wire [8:0] pa = busy ? bf0 : rbr;
    wire [8:0] pb = busy ? bf1 : wr_addr;

    // ------------------------------------------------------------ 蝶形
    reg signed [23:0] x0_re, x0_im, x1_re, x1_im;
    reg signed [15:0] t_re, t_im;

    // 每级都右移 1 位最保险，但"分析→掩蔽→合成"要过两次变换，
    // 20 次移位把 24 位里的有效位吃光，往返误差能到 10%。
    // 所以只在后 5 级缩放：前 5 级最多涨 32 倍，输入取 16 位音频左移 1 位
    // （≤2^16），5 级后 ≤2^21、相加 ≤2^22，24 位放得下。
    wire scale_en = (stage >= 4'd4);

    wire signed [24:0] d_re = x0_re - x1_re;
    wire signed [24:0] d_im = x0_im - x1_im;
    wire signed [24:0] s_re = x0_re + x1_re;
    wire signed [24:0] s_im = x0_im + x1_im;
    // (a+bj)(c+dj)，Q15。线名带 _p 是为了跟 RAM 的 m_re / m_im 区分开。
    wire signed [48:0] m_re_p = d_re * t_re - d_im * t_im;
    wire signed [48:0] m_im_p = d_re * t_im + d_im * t_re;
    // 先解 Q15（带舍入，别用截断，否则负半周会累积直流偏置）
    wire signed [48:0] sh_re = (m_re_p + 49'sd16384) >>> 15;
    wire signed [48:0] sh_im = (m_im_p + 49'sd16384) >>> 15;

    // 写回值：a 槽放"和"，b 槽放"差 × 旋转因子"
    wire signed [23:0] out0_re = scale_en ? s_re[24:1] : s_re[23:0];
    wire signed [23:0] out0_im = scale_en ? s_im[24:1] : s_im[23:0];
    wire signed [23:0] acc1_re = scale_en ? sh_re[24:1] : sh_re[23:0];
    wire signed [23:0] acc1_im = scale_en ? sh_im[24:1] : sh_im[23:0];

    // ------------------------------------------------------------ RAM 端口
    // 蝶形写只在 ph==2 拉高一拍；地址用**当拍**的 pa/pb，而 base/jj 是
    // 非阻塞更新的，所以这一拍写的还是刚读出来的那两个地址 —— 原址成立。
    wire bf_we  = (state == S_BFLY) && (ph == 2'd2);
    wire we_a   = bf_we;
    wire we_b   = bf_we | (wr_en & ~busy);

    // B 口忙时写蝶形结果、空闲时写外部输入
    wire signed [23:0] din_b_re = busy ? acc1_re : wr_re;
    // 输入取共轭要用**当前**的 inverse，不是上一帧锁存的 inv_r：
    // inv_r 在 start 时才更新，而装载发生在 start 之前，
    // 用 inv_r 会让"正向变换"误把输入共轭掉（复谱合成段会直接错）。
    wire signed [23:0] din_b_im = busy ? acc1_im : (inverse ? -wr_im : wr_im);

    reg signed [23:0] d0_re, d0_im, d1_re, d1_im;

    // --- A 口：读 a / 写 a（read-first：写的同时读出旧值，反正这一拍不用）
    always @(posedge clk) begin
        if (we_a) m_re[pa] <= out0_re;
        d0_re <= m_re[pa];
    end
    always @(posedge clk) begin
        if (we_a) m_im[pa] <= out0_im;
        d0_im <= m_im[pa];
    end

    // --- B 口：读 b / 写 b（装载也走这个口，所以跟蝶形写放同一个块里）
    always @(posedge clk) begin
        if (we_b) m_re[pb] <= din_b_re;
        d1_re <= m_re[pb];
    end
    always @(posedge clk) begin
        if (we_b) m_im[pb] <= din_b_im;
        d1_im <= m_im[pb];
    end

    // 结果输出：两级寄存器（d0 -> rd），所以外部给地址后要等两拍。
    // IFFT 输出要再取一次共轭。
    always @(posedge clk) begin
        rd_re <= d0_re;
        rd_im <= inv_r ? -d0_im : d0_im;
    end

    // ------------------------------------------------------------ 控制
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_IDLE; busy <= 1'b0; done <= 1'b0;
            stage <= 4'd0; base <= 9'd0; jj <= 9'd0; ph <= 2'd0;
            inv_r <= 1'b0;
            x0_re <= 24'sd0; x0_im <= 24'sd0; x1_re <= 24'sd0; x1_im <= 24'sd0;
            t_re <= 16'sd0; t_im <= 16'sd0;
        end else begin
            done <= 1'b0;

            case (state)
                S_IDLE: begin
                    busy <= 1'b0;
                    if (start) begin
                        inv_r <= inverse;
                        stage <= 4'd0; base <= 9'd0; jj <= 9'd0; ph <= 2'd0;
                        busy  <= 1'b1;
                        state <= S_BFLY;
                    end
                end

                S_BFLY: begin
                    case (ph)
                        2'd0: ph <= 2'd1;                      // 地址已经给出
                        2'd1: begin                            // 取数 + 锁旋转因子
                            x0_re <= d0_re;  x0_im <= d0_im;
                            x1_re <= d1_re;  x1_im <= d1_im;
                            t_re  <= tw_re;  t_im  <= tw_im;
                            ph    <= 2'd2;
                        end
                        default: begin                          // 写回 + 推进
                            ph <= 2'd0;
                            if (jj == half - 1) begin
                                jj <= 9'd0;
                                if (blk_end >= 11'd512) begin
                                    base <= 9'd0;
                                    if (stage == NB - 1) state <= S_DONE;
                                    else                 stage <= stage + 4'd1;
                                end else begin
                                    base <= base + blk;
                                end
                            end else begin
                                jj <= jj + 10'd1;
                            end
                        end
                    endcase
                end

                S_DONE: begin
                    busy  <= 1'b0;
                    done  <= 1'b1;
                    state <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
