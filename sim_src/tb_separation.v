// ============================================================================
//  tb_separation.v   separation_core 的裸仿真
//
//  激励由 tool/gen_sep_tb_data.py 生成（sim_src/tb_sep_in_l.hex / _r.hex）。
//  只跑少量样本，目的是确认**状态机能走完一帧并且时序握手没错**，
//  不追求分离质量——那个由 tool/sim_separation.py 负责。
//
//  跑法：
//      xvlog rtl/*.v sim_src/tb_separation.v
//      xelab tb_separation -s tb_sep_sim
//      xsim tb_sep_sim -R
// ============================================================================
`timescale 1ns / 1ps

module tb_separation;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    reg en = 1'b1;
    reg din_valid = 1'b0;
    reg signed [15:0] din_l = 16'sd0;
    reg signed [15:0] din_r = 16'sd0;

    wire signed [15:0] dout_l, dout_r;
    wire        dout_valid;
    wire [9:0]  f0_m_lag, f0_f_lag;
    wire [1:0]  who;
    wire        busy;

    separation_core #(.DELAY(1280)) uut (
        .clk(clk), .rst_n(rst_n), .en(en),
        .din_l(din_l), .din_r(din_r), .din_valid(din_valid),
        .dout_l(dout_l), .dout_r(dout_r), .dout_valid(dout_valid),
        .f0_m_lag(f0_m_lag), .f0_f_lag(f0_f_lag), .who(who), .busy(busy)
    );

    always #10 clk = ~clk;                     // 50 MHz

    reg [15:0] mem_l [0:4095];
    reg [15:0] mem_r [0:4095];

    integer i, k;
    integer nsmp;
    integer fout, fpre, fdbg;
    integer cyc_per_sample = 2048;             // 50 MHz / 24.414 kHz
    integer cycle_budget;

    initial begin
        $readmemh("sim_src/tb_sep_in_l.hex", mem_l);
        $readmemh("sim_src/tb_sep_in_r.hex", mem_r);

        fout = $fopen("sim_src/tb_sep_out.txt", "w");
        if (fout == 0) begin
            $display("TB: cannot open output file");
            $finish;
        end

        nsmp = 4096;                           // DELAY 1280 + 约 11 帧
        cycle_budget = nsmp * cyc_per_sample + 400000;

        #100 rst_n = 1'b1;
        #200;

        for (i = 0; i < nsmp; i = i + 1) begin
            din_l <= mem_l[i];
            din_r <= mem_r[i];
            din_valid <= 1'b1;
            #20;                               // 一个系统时钟
            din_valid <= 1'b0;
            if (i >= 1280) begin
                $fwrite(fout, "%0d %0d %0d %0d %0d %0d\n",
                        i, dout_l, dout_r, who, f0_m_lag, f0_f_lag);
            end
            #(20 * cyc_per_sample - 20);       // 等到下一个采样点
        end

        $fclose(fout);
        $display("TB: done  who=%0d  lag_m=%0d  lag_f=%0d", who, f0_m_lag, f0_f_lag);
        $display("TB: last dout_l=%0d dout_r=%0d", dout_l, dout_r);
        $finish;
    end

    // 状态监视：状态机每跳一步打一行，用来定位"跑完了但没判决"这类问题
    integer dbg_en = 1;
    always @(uut.state) begin
        if (dbg_en && uut.state != 0)
            $display("%9t S=%0d idx=%0d wp=%0d ws=%0d who_r=%0d lm=%0d lf=%0d",
                     $time, uut.state, uut.idx, uut.wp, uut.win_start,
                     uut.who_r, uut.lm_r, uut.lf_r);
    end
    always @(posedge uut.frame_req) begin
        if (dbg_en)
            $display("%9t FRAME_REQ wp=%0d win_start=%0d", $time, uut.wp, uut.win_start);
    end
    always @(posedge uut.f_done) begin
        if (dbg_en) $display("%9t FFT_DONE S=%0d", $time, uut.state);
    end

    // 分析段结果：|V| 和 |side| 的前几个 bin，用来确认 FFT/掩蔽有没有塌
    always @(uut.state) begin
        if (dbg_en && uut.state == 8)
            $display("  ANA sp_re=%0d,%0d,%0d,%0d  sd_mag=%0d,%0d,%0d,%0d  mid_v=%0d c=%0d",
                     uut.sp_re[1], uut.sp_re[2], uut.sp_re[3], uut.sp_re[4],
                     uut.sd_mag[1], uut.sd_mag[2], uut.sd_mag[3], uut.sd_mag[4],
                     uut.mid_v, uut.c_q15);
    end
    // 加窗后的 FFT 输入（只记第一帧）：定位"谱比模型小"是哪一步开始偏的
    integer fwin, win_n = 0;
    initial fwin = $fopen("sim_src/tb_sep_win.txt", "w");
    always @(posedge clk) begin
        if (dbg_en && uut.state == 1 && uut.f_wen && win_n < 1024) begin
            $fwrite(fwin, "%0d %0d %0d %0d\n",
                    uut.f_waddr, uut.mid_v, uut.win_val, uut.f_wre);
            win_n <= win_n + 1;
        end
    end

    // 掩蔽之前：整条 mid/side 谱（原始，未掩蔽）
    initial fpre = $fopen("sim_src/tb_sep_pre.txt", "w");
    always @(uut.state) begin
        if (dbg_en && uut.state == 7) begin
            $fwrite(fpre, "FRAME ws=%0d\n", uut.win_start);
            for (k = 0; k < 512; k = k + 1)
                $fwrite(fpre, "%0d %0d %0d %0d\n", k,
                        uut.sp_re[k], uut.sp_im[k], uut.sd_mag[k]);
        end
    end

    // 判决现场：整条 acf + 门限/峰值，落在 S_CLRC（每帧必经）
    initial fdbg = $fopen("sim_src/tb_sep_acf.txt", "w");
    always @(uut.state) begin
        if (dbg_en && uut.state == 16) begin
            $display("%9t DECIDE ws=%0d acf0=%0d thr=%0d pm=%0d lm=%0d pf=%0d lf=%0d -> who=%0d lm_r=%0d lf_r=%0d",
                     $time, uut.win_start, uut.acf0, uut.thr, uut.peak_m,
                     uut.lag_m, uut.peak_f, uut.lag_f,
                     uut.who_r, uut.lm_r, uut.lf_r);
            $fwrite(fdbg, "FRAME ws=%0d acf0=%0d who=%0d lm=%0d lf=%0d\n",
                    uut.win_start, uut.acf0, uut.who_r, uut.lm_r, uut.lf_r);
            for (k = 0; k < 1024; k = k + 1)
                $fwrite(fdbg, "%0d %0d\n", k, uut.acf[k]);
        end
    end

    // 兜底：跑太久就退出，避免仿真挂死吃满时间
    initial begin
        #(20 * 4096 * 2048 + 20 * 400000);
        $display("TB: TIMEOUT");
        $finish;
    end

endmodule
