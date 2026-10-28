`timescale 1ns / 1ps
// 纯音测试：这是唯一能验证**旋转因子寻址**的判据。
// 直流测试测不出 twiddle 对错（任何 twiddle 错误下 DC 都正确），
// 所以必须再喂一个 bin k 上的纯余弦，看能量是否**只**落在 bin k。
module tb_fft512_tone;
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

    localparam A    = 24'sd4096;
    localparam KBIN = 8;
    // 期望：512 点、末 5 级各右移 1（共 1/32）=> bin k 幅度 = N*A/2/32 = A*8
    localparam EXP  = A * 8;

    integer i, k, bad;
    reg signed [23:0] bk, mx;

    initial begin
        repeat(8) @(posedge clk); rst_n=1; repeat(8) @(posedge clk);
        for (i=0;i<512;i=i+1) begin
            wa  = i;
            wre = (A * $cos(2.0*3.14159265358979*KBIN*i/512.0)) / 1;
            wim = 0;
            wr_en = 1;
            @(posedge clk);
        end
        wr_en=0;
        @(posedge clk); start=1; @(posedge clk); start=0;
        @(posedge done);

        ra=KBIN; repeat(3) @(posedge clk);
        bk = rre;
        mx = 0;
        for (k=0;k<256;k=k+1) begin
            if (k != KBIN) begin
                ra=k; repeat(3) @(posedge clk);
                if (rre > mx)  mx = rre;
                if (-rre > mx) mx = -rre;
                if (rim > mx)  mx = rim;
                if (-rim > mx) mx = -rim;
            end
        end

        $display("纯音测试：bin %0d，A=%0d，期望幅度=%0d", KBIN, A, EXP);
        $display("  实测 bin%0d = %0d", KBIN, bk);
        $display("  其它 bin 的最大泄漏 = %0d", mx);

        bad = 0;
        if (bk < (EXP*85)/100 || bk > (EXP*115)/100) bad = bad + 1;
        if (mx > EXP/8) bad = bad + 1;                 // 泄漏 < 12.5%
        if (bad==0) $display("RESULT: ALL PASS");
        else        $display("RESULT: HAS FAILURE");
        $finish;
    end
endmodule
