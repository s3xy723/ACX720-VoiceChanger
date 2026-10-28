`timescale 1ns / 1ps
// ============================================================================
//  tb_ola_round.v   纯 STFT 往返的隔离测试（不含任何增益逻辑）
//
//  age_core 的 β=1 判据过不了，但那时 g 恒等于 1、Y 应该等于 X，
//  所以嫌疑落在"帧流水"本身：加窗 / FFT / IFFT / 重叠相加 / 定标。
//  这里把这条链路单独搭一遍（用与 age_core 完全相同的算式），
//  如果它都不透明，就说明公式里有错，与年龄算法无关。
// ============================================================================
module tb_ola_round;
    reg clk=0, rst_n=0;
    always #10 clk = ~clk;

    localparam N = 512;
    localparam HOP = 128;

    // ---- 被测：加窗 -> FFT -> IFFT -> 加窗 -> OLA
    reg start=0, inv=0, wr_en=0;
    reg [8:0] wa=0, ra=0;
    reg signed [47:0] wre_w=0;
    reg signed [23:0] wre=0, wim=0;
    wire busy, done;
    wire signed [23:0] rre, rim;
    fft512 u(.clk(clk),.rst_n(rst_n),.start(start),.inverse(inv),.busy(busy),.done(done),
             .wr_en(wr_en),.wr_addr(wa),.wr_re(wre),.wr_im(wim),.rd_addr(ra),.rd_re(rre),.rd_im(rim));

    reg [9:0] win_addr; wire [15:0] win_val;
    win1024 uw(.addr(win_addr), .w(win_val));

    (* ram_style="block" *) reg signed [23:0] acc [0:2047];
    reg signed [15:0] smp [0:511];
    integer mi;
    initial begin
        for (mi=0; mi<512; mi=mi+1) smp[mi]=0;
        for (mi=0; mi<2048; mi=mi+1) acc[mi]=0;
    end

    // 用直流 8000 填一帧（最容易核对定标）
    integer i, j;
    reg signed [15:0] xv;
    reg [10:0] base;
    reg signed [23:0] yv;
    real err;

    task load_frame;
        input [10:0] b;
        begin
            for (i=0;i<N;i=i+1) begin
                win_addr = {i[8:0], 1'b0};
                #1;                                   // 等窗 ROM
                xv = smp[(b + i) & 511];
                wre_w = ($signed(xv) * $signed({1'b0, win_val})) >>> 15;  // 48 位上下文
                wre   = wre_w[23:0];
                wim = 0;
                wa = i; wr_en = 1;
                if (i==0 || i==1 || i==255 || i==256 || i==511)
                    $display("  load i=%0d smp=%0d win=%0d wre=%0d",
                             i, xv, win_val, wre);
                @(posedge clk);
            end
            wr_en = 0;
        end
    endtask

    initial begin
        repeat(10) @(posedge clk); rst_n=1; repeat(10) @(posedge clk);
        for (i=0;i<512;i=i+1) smp[i] = 8000;
        base = 0;

        // 只跑一帧，看 OLA 之后 acc 里到底是多少
        load_frame(base);
        @(posedge clk); start=1; inv=0; @(posedge clk); start=0;
        @(posedge done); @(posedge clk);
        // 先看正变换对不对：X[0] 应 = 8000*256/32 = 64000
        ra=0; repeat(3) @(posedge clk);
        $display("正变换 X[0] = %0d（期望 64000）", rre);
        ra=1; repeat(3) @(posedge clk);
        $display("正变换 X[1] = %0d（Hann 旁瓣，应远小于 X[0]）", rre);
        // ★ 逆变换必须**重新装载**（装载时取第一次共轭）。age_core 里这一步
        //   由 S_WRAP 写 Y 顺带完成；这里直接把谱原样读回再写一遍。
        begin : reload
            integer k;
            reg signed [23:0] vr, vi;
            inv = 1;
            for (k=0;k<N;k=k+1) begin
                ra = k; repeat(3) @(posedge clk);
                vr = rre; vi = rim;
                wa = k; wre = vr; wim = vi; wr_en = 1;
                @(posedge clk);
            end
            wr_en = 0;
        end
        @(posedge clk); start=1; inv=1; @(posedge clk); start=0;
        @(posedge done); @(posedge clk);

        // 读回第 idx=256（窗顶）处的值
        ra = 256; repeat(3) @(posedge clk);
        $display("IFFT 后 idx=256 的实部 = %0d（期望 ≈ 0.5*8000*1.0 = 4000）", rre);

        // 合成加窗 + 累加（只算一帧，acc 里就只有一帧的贡献）
        begin : syn
            integer k;
            reg signed [35:0] m;
            reg [10:0] ai;
            for (k=0;k<N;k=k+1) begin
                ra = k; win_addr = {k[8:0], 1'b0};
                repeat(3) @(posedge clk);
                m  = ($signed(rre) * $signed({1'b0, win_val})) >>> 15;
                ai = (base + k) & 2047;
                acc[ai] = acc[ai] + m[23:0];
            end
        end

        $display("一帧累加后 acc[base+256] = %0d", acc[(base+256) & 2047]);
        $display("  若四帧重叠则 ×1.5 = %0d；乘 4/3 恢复 = %0d",
                 (acc[(base+256)&2047]*3)/2, ((acc[(base+256)&2047]*3)/2*4)/3);
        $display("  输入是 %0d，所以最终应回到 8000 附近", 8000);
        $finish;
    end
endmodule
