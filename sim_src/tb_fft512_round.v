`timescale 1ns / 1ps
// 测 fft512 的往返增益：FFT 后立刻 IFFT，看输出 = 输入 × 多少
module tb_fft512_round;
    reg clk=0,rst_n=0; always #10 clk=~clk;
    reg start=0,inv=0,wr_en=0; reg [8:0] wa=0,ra=0;
    reg signed [23:0] wre=0,wim=0;
    wire busy,done; wire signed [23:0] rre,rim;
    fft512 dut(.clk(clk),.rst_n(rst_n),.start(start),.inverse(inv),.busy(busy),.done(done),
               .wr_en(wr_en),.wr_addr(wa),.wr_re(wre),.wr_im(wim),.rd_addr(ra),.rd_re(rre),.rd_im(rim));
    localparam A=24'sd4096;
    integer i; reg signed [23:0] y0;
    initial begin
        repeat(8) @(posedge clk); rst_n=1; repeat(8) @(posedge clk);
        for(i=0;i<512;i=i+1) begin wa=i; wre=A; wim=0; wr_en=1; @(posedge clk); end
        wr_en=0; @(posedge clk); start=1; inv=0; @(posedge clk); start=0;
        @(posedge done);
        @(posedge clk); start=1; inv=1; @(posedge clk); start=0;
        @(posedge done);
        ra=0; repeat(3) @(posedge clk);
        y0=rre;
        $display("往返测试：输入直流 %0d -> 输出 %0d", A, y0);
        $display("  往返增益 = 输出/输入 = %0d/%0d", y0, A);
        $display("  若 = %0d 则是 1/1024；若 = %0d 则是 N/1024 = 1/2", A/1024, A/2);
        $finish;
    end
endmodule
