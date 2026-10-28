`timescale 1ns / 1ps
// 单脉冲测试：输入一个单位脉冲，OLA 正确时输出仍是一个（被 Σw² 缩放的）脉冲。
// 若输出是一团 512 样本宽的拖尾，说明帧叠加不完整。
module tb_imp;
    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;

    reg signed [15:0] din = 0;
    reg din_valid = 0;
    wire signed [15:0] dout;
    wire dout_valid;

    age_core dut(.clk(clk), .rst_n(rst_n), .en(1'b1), .beta16(16'd16384),
                 .din(din), .din_valid(din_valid), .dout(dout), .dout_valid(dout_valid));

    integer i, fo;
    initial begin
        fo = $fopen("imp_out.txt", "w");
        repeat(20) @(posedge clk); rst_n = 1; repeat(20) @(posedge clk);
        for (i = 0; i < 4200; i = i + 1) begin
            din = (i == 1500) ? 16'sd20000 : ((i % 200 == 0) ? 16'sd100 : 16'sd0);
            din_valid = 1; @(posedge clk); din_valid = 0;
            repeat(2047) @(posedge clk);
            $fdisplay(fo, "%0d", $signed(dout));
        end
        $fclose(fo);
        $display("IMP done");
        $finish;
    end
endmodule
