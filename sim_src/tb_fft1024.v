// ============================================================================
//  tb_fft1024.v   fft1024 的裸仿真
//
//  激励由 tool/gen_fft_tb_data.py 生成。目的：确认 RTL 本体（含 fft_twiddle
//  ROM）和 tool/sim_fft1024.py 的模型逐点一致 —— 之前只做过 Python 对 numpy，
//  RTL 从没在仿真里跑过，旋转因子 ROM 的内容也从未核对。
//
//  除 rd_re/rd_im 之外还顺手 dump 一份 RAM 直读（tb_fft_mem.txt）。
//  这一列看着多余，但真出事时能一秒分清"蝶形算错了"还是"读路径错位了"：
//  频谱整体循环移一个 bin 这两种情况的波形几乎一样，只有 RAM 直读能判别。
//
//  跑法：
//      xvlog rtl/fft_twiddle.v rtl/fft1024.v sim_src/tb_fft1024.v
//      xelab tb_fft1024 -s tb_fft_sim --timescale 1ns/1ps
//      xsim tb_fft_sim -R
//      python tool/check_fft_sim.py
// ============================================================================
`timescale 1ns / 1ps

module tb_fft1024;

    reg clk = 1'b0;
    reg rst_n = 1'b0;

    reg         start = 1'b0;
    reg         inverse = 1'b0;
    wire        busy, done;
    reg         wr_en = 1'b0;
    reg  [9:0]  wr_addr = 10'd0;
    reg  signed [23:0] wr_re = 24'sd0;
    reg  signed [23:0] wr_im = 24'sd0;
    reg  [9:0]  rd_addr = 10'd0;
    wire signed [23:0] rd_re, rd_im;

    fft1024 uut (
        .clk(clk), .rst_n(rst_n),
        .start(start), .inverse(inverse), .busy(busy), .done(done),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_re(wr_re), .wr_im(wr_im),
        .rd_addr(rd_addr), .rd_re(rd_re), .rd_im(rd_im)
    );

    always #10 clk = ~clk;

    reg signed [23:0] mem_re [0:1023];
    reg signed [23:0] mem_im [0:1023];

    integer i;
    integer fout;
    integer fmemo;
    integer inv_case;

    // keep=0 时只跑不记盘。
    // xsim 在复位后**第一次**往数据 RAM 写会被丢掉（条件完全相同，只有首次
    // 复现），第一帧结果全是 x；硬件里 BRAM 上电为 0，不会有这事。
    // 所以先跑一次作废事务预热，再记录真正要比的那两次。
    task do_one;
        input integer inv;
        input integer keep;
        begin
            inverse = inv[0];
            // 装载
            for (i = 0; i < 1024; i = i + 1) begin
                if (i == 1)
                    $display("  [i=1] m0=%0d state=%0d wen=%0d",
                             uut.m_re[0], uut.state, wr_en);
                wr_addr = i;
                wr_re   = mem_re[i];
                wr_im   = mem_im[i];
                wr_en   = 1'b1;
                @(posedge clk);
            end
            wr_en = 1'b0;
            $display("  load done: mem0=%0d m0=%0d m1=%0d state=%0d busy=%0d",
                     mem_re[0], uut.m_re[0], uut.m_re[1],
                     uut.state, busy);
            // 启动
            start = 1'b1;
            @(posedge clk);
            start = 1'b0;
            // 等 done
            @(posedge done);
            @(posedge clk);
            @(posedge clk);
            // 读出（rd_re/rd_im 是寄存器输出，给地址后下一拍有效）
            if (keep != 0) begin
                $fwrite(fout, "CASE inv=%0d\n", inv);
                $fwrite(fmemo, "CASE inv=%0d\n", inv);
            end
            for (i = 0; i < 1024; i = i + 1) begin
                rd_addr = i;
                @(posedge clk);
                @(posedge clk);
                // $fwrite 是在 posedge 的 Active 区跑的，不加这一小段延时
                // 读到的是**这个沿之前**的 rd_re（整整差一拍，谱就整体移一个
                // bin）。#1 让非阻塞赋值先落定，采到的才是当拍值。
                #1;
                if (keep != 0) begin
                    $fwrite(fout, "%0d %0d\n", rd_re, rd_im);
                    // 直接读 RAM，用来区分"读路径错位"和"蝶形数据错位"
                    $fwrite(fmemo, "%0d %0d\n", uut.m_re[uut.rbr],
                            uut.m_im[uut.rbr]);
                end
            end
        end
    endtask

    // 抓第一个蝶形：这次写回时 x0/x1 若还是 x，就会把 x 一路传播到输出
    integer probe = 0;
    always @(posedge clk) begin
        if (probe == 0 && uut.state == 1 && uut.stage == 0 && uut.ph == 2) begin
            probe <= 1;
            $display("  BF0: x0=%0d x1=%0d m0=%0d m512=%0d t_re=%0d",
                     uut.x0_re, uut.x1_re, uut.m_re[0], uut.m_re[512],
                     uut.t_re);
        end
    end

    initial begin
        $readmemh("sim_src/tb_fft_in_re.hex", mem_re);
        $readmemh("sim_src/tb_fft_in_im.hex", mem_im);
        fout  = $fopen("sim_src/tb_fft_out.txt", "w");
        fmemo = $fopen("sim_src/tb_fft_mem.txt", "w");
        if (fout == 0) begin
            $display("TB: cannot open output");
            $finish;
        end

        #100 rst_n = 1'b1;
        #200;

        do_one(0, 0);                      // 作废：预热
        do_one(0, 1);
        $display("TB: forward done");
        do_one(1, 1);
        $display("TB: inverse done");

        $fclose(fout);
        $fclose(fmemo);
        $finish;
    end

    initial begin
        #20000000;
        $display("TB: TIMEOUT");
        $finish;
    end

endmodule
