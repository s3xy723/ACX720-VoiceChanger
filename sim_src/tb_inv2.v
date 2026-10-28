`timescale 1ns / 1ps
// 最小裁决：fft512 的"正向 → 读回 → 重新装载 → 反向"整圈对不对。
//
// 输入不加窗的直流 4096。理论值：
//   正向  X[0] = 512*4096/32 = 65536，其余 0
//   反向  往返增益 1/2  =>  输出处处 2048
//
// 这条测试故意把每个环节拆开打印，用来区分"反向本身错"还是"我读地址的方式错"。
module tb_inv2;
    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;

    reg  start = 0, inv = 0, wr_en = 0;
    reg  [8:0] wa = 0, ra = 0;
    reg  signed [23:0] wre = 0, wim = 0;
    wire busy, done;
    wire signed [23:0] rre, rim;

    fft512 dut(.clk(clk), .rst_n(rst_n), .start(start), .inverse(inv),
               .busy(busy), .done(done), .wr_en(wr_en), .wr_addr(wa),
               .wr_re(wre), .wr_im(wim), .rd_addr(ra), .rd_re(rre), .rd_im(rim));

    localparam A = 24'sd4096;

    // 4 段读：0 给地址 / 1,2 等两拍 / 3 取数（与 separation_core 的 rph 相同）
    reg [1:0] rph;
    reg signed [23:0] rd_re_r, rd_im_r;

    integer i, k, bad;
    reg signed [23:0] x0, x8, y0, y1, y256;

    task rd_one;
        input [8:0] a;
        begin
            ra = a; rph = 0;
            @(posedge clk);           // rph0 -> 1
            rph = 1;
            @(posedge clk);           // 1 -> 2
            rph = 2;
            @(posedge clk);           // 2 -> 3
            rph = 3;
            @(posedge clk);           // 这一拍 rd_re 有效
            rd_re_r = rre; rd_im_r = rim;
        end
    endtask

    initial begin
        repeat(8) @(posedge clk); rst_n = 1; repeat(8) @(posedge clk);

        // ---- 装载直流
        // 余弦 bin8：载入 cos(2*pi*8*i/512)。相位信息全靠它，
        // 直流测不出"反向是不是变成了希尔伯特变换"。
        for (i = 0; i < 512; i = i + 1) begin
            wa = i;
            wre = $rtoi(24000.0 * $cos(2.0*3.14159265358979*8.0*i/512.0));
            wim = 0; wr_en = 1;
            @(posedge clk);
        end
        wr_en = 0; @(posedge clk);

        // ---- 正向
        start = 1; inv = 0; @(posedge clk); start = 0;
        @(posedge done); @(posedge clk);
        rd_one(9'd0);   x0 = rd_re_r;
        rd_one(9'd8);   x8 = rd_re_r;
        $display("正向: X[0]=%0d (期望 65536)  X[8]=%0d (期望 0)", x0, x8);

        // ---- 原样读回再写回（inv=1，装载时取共轭）
        inv = 1;
        for (k = 0; k < 512; k = k + 1) begin
            rd_one(k[8:0]);
            wa = k[8:0]; wre = rd_re_r; wim = rd_im_r; wr_en = 1;
            @(posedge clk);
        end
        wr_en = 0; @(posedge clk);

        // ---- 反向
        start = 1; inv = 1; @(posedge clk); start = 0;
        @(posedge done); @(posedge clk);
        rd_one(9'd0);   y0 = rd_re_r;
        rd_one(9'd1);   y1 = rd_re_r;
        rd_one(9'd2);   y256 = rd_re_r;
        $display("反向: y[0]=%0d  y[1]=%0d  y[2]=%0d", y0, y1, y256);
        $display("  期望 = 0.5*24000*cos(...) : y[0]=12000 y[1]=11972 y[2]=11888");
        $display("  若是希尔伯特(相位转90度)：y[0]=0 y[1]=-1111 y[2]=-2221");

        bad = 0;
        if (y0 < 11000 || y0 > 13000) bad = bad + 1;
        if (y1 < 11000 || y1 > 13000) bad = bad + 1;
        if (bad == 0) $display("RESULT: ALL PASS");
        else          $display("RESULT: HAS FAILURE");
        $finish;
    end
endmodule
