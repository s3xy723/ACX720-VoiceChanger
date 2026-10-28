`timescale 1ns / 1ps
module tb_age_probe;
    reg clk=0, rst_n=0;
    always #10 clk = ~clk;
    reg [15:0] beta16 = 16'd16384;
    reg signed [15:0] din=0;
    reg din_valid=0;
    wire signed [15:0] dout;
    wire dout_valid;
    integer i,j;

    age_core dut(.clk(clk),.rst_n(rst_n),.en(1'b1),.beta16(beta16),
                 .din(din),.din_valid(din_valid),.dout(dout),.dout_valid(dout_valid));

    initial begin
        repeat(20) @(posedge clk); rst_n=1; repeat(20) @(posedge clk);
        for (i=0;i<1600;i=i+1) begin
            din = 8000;                    // 直流，最容易看定标
            din_valid=1; @(posedge clk); din_valid=0;
            if (i%200==0)
                $display("n=%4d state=%0d idx=%0d win_start=%5d out_ptr=%5d acc[out_ptr]=%0d dout=%0d",
                    i, dut.state, dut.idx, dut.win_start, dut.out_ptr,
                    dut.acc[dut.out_ptr], dout);
            for (j=0;j<2047;j=j+1) @(posedge clk);
        end
        $finish;
    end
endmodule
