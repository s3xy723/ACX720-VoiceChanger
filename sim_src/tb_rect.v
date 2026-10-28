`timescale 1ns / 1ps
// 矩形窗隔离实验：如果矩形窗下重建完美，说明问题在窗；否则在帧内容。
module tb_rect;
    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;

    reg signed [15:0] din = 0;
    reg din_valid = 0;
    wire signed [15:0] dout;
    wire dout_valid;

    age_core #(.RECT(1)) dut(
        .clk(clk), .rst_n(rst_n), .en(1'b1), .beta16(16'd16384),
        .din(din), .din_valid(din_valid), .dout(dout), .dout_valid(dout_valid));

    integer i, fo, fi;
    initial begin
        fo = $fopen("rect_out.txt", "w");
        fi = $fopen("rect_in.txt", "w");
        repeat(20) @(posedge clk); rst_n = 1; repeat(20) @(posedge clk);
        for (i = 0; i < 2600; i = i + 1) begin
            din = $rtoi(12000.0 * $sin(2.0*3.14159265358979*200.0*i/24414.0)
                      +  6000.0 * $sin(2.0*3.14159265358979*1234.0*i/24414.0));
            $fdisplay(fi, "%0d", din);
            din_valid = 1; @(posedge clk); din_valid = 0;
            repeat(2047) @(posedge clk);
            $fdisplay(fo, "%0d", $signed(dout));
        end
        $fclose(fo); $fclose(fi);
        $display("RECT done");
        $finish;
    end
endmodule
