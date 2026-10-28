// 只实例化 pitch_shift 的定向 debug TB：打印内部相位与抽头延迟
`timescale 1ns / 1ps

module tb_pitch_probe;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    reg cfg_reset = 1'b1;
    reg signed [8:0] dphase = 9'sd0;
    reg signed [15:0] din = 16'sd0;
    reg din_valid = 1'b0;
    wire signed [15:0] dout;
    wire dout_valid;

    pitch_shift #(.AW(12)) dut (
        .clk(clk), .rst_n(rst_n), .cfg_reset(cfg_reset),
        .dphase(dphase), .din(din), .din_valid(din_valid),
        .dout(dout), .dout_valid(dout_valid)
    );

    always #10 clk = ~clk;

    integer n;
    integer stop_at;

    initial begin
        stop_at = 40;
        rst_n = 1'b0;
        repeat (20) @(posedge clk);
        rst_n = 1'b1;
        @(posedge clk);
        cfg_reset = 1'b1;
        @(posedge clk);
        cfg_reset = 1'b0;
        dphase = 9'sd0 - 9'sd32;          // 期望 phase 每步减 32
        din = 16'sd1000;
        for (n = 0; n < stop_at; n = n + 1) begin
            din_valid <= 1'b1;
            @(posedge clk);
            din_valid <= 1'b0;
            $write("n=%0d phase=%0d signed=%0d ", n, dut.phase, $signed(dut.phase));
            $write("dly_a=%0d dly_b=%0d ", dut.dly_a_int, dut.dly_b_int);
            $write("X=%0d Y=%0d ", dut.X, dut.Y);
            $display("base_a=%0d base_b=%0d dout=%0d", dut.base_a, dut.base_b, dout);
            repeat (2) @(posedge clk);
        end
        $finish;
    end

endmodule
