`timescale 1ns / 1ps
module tb_fft512;
    reg clk=0, rst_n=0;
    always #10 clk = ~clk;

    reg start=0, inv=0, wr_en=0;
    reg [8:0] wa=0, ra=0;
    reg signed [23:0] wre=0, wim=0;
    wire busy, done;
    wire signed [23:0] rre, rim;

    fft512 dut(.clk(clk),.rst_n(rst_n),.start(start),.inverse(inv),
               .busy(busy),.done(done),.wr_en(wr_en),.wr_addr(wa),
               .wr_re(wre),.wr_im(wim),.rd_addr(ra),.rd_re(rre),.rd_im(rim));

    localparam A = 24'sd4096;
    integer i, k, bad;
    reg signed [23:0] b0, mx;

    initial begin
        repeat(8) @(posedge clk); rst_n=1; repeat(8) @(posedge clk);
        // 装入 512 个直流
        for (i=0;i<512;i=i+1) begin
            wa=i; wre=(A*$cos(2.0*3.14159265358979*8.0*i/512.0))/1; wim=0; wr_en=1; @(posedge clk);
        end
        wr_en=0;
        @(posedge clk); start=1; @(posedge clk); start=0;
        // 等 done
        @(posedge done);
        // 读 bin0（给地址后等两拍）
        ra=8; repeat(3) @(posedge clk);
        b0 = rre;
        // 扫一遍找最大非 DC 分量
        mx = 0;
        for (k=0;k<256;k=k+1) begin if (k==8) begin ra=k; repeat(3) @(posedge clk); end else begin
            ra=k; repeat(3) @(posedge clk);
            if (rre > mx) mx = rre;
            if (-rre > mx) mx = -rre;
            if (rim > mx) mx = rim;
            if (-rim > mx) mx = -rim;
        end
        $display("纯音测试（bin 8，cos）：A=%0d，期望 bin8 幅度 = %0d", A, A*8);
        $display("  实测 bin0 = %0d", b0);
        $display("  最大非 DC 分量 = %0d", mx);
        bad = 0;
        if (b0 < (A*8)*90/100 || b0 > (A*8)*110/100) bad = bad + 1;
        if (mx > (A*8)/8) bad = bad + 1;
        if (bad==0) $display("RESULT: ALL PASS");
        else        $display("RESULT: HAS FAILURE");
        $finish;
    end
endmodule
