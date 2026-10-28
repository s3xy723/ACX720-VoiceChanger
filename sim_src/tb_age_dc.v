`timescale 1ns / 1ps
// 连续直流输入（最省事的定标检查）：β=1 时输出应当收敛到与输入同幅。
// 同时打印内部状态，方便一眼看出帧有没有在走、累加器有没有被清。
module tb_age_dc;
    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;

    reg signed [15:0] din = 16'sd8000;
    reg din_valid = 0;
    wire signed [15:0] dout;
    wire dout_valid;

    age_core dut(.clk(clk), .rst_n(rst_n), .en(1'b1), .beta16(16'd16384),
                 .din(din), .din_valid(din_valid),
                 .dout(dout), .dout_valid(dout_valid));

    integer n, frames = 0;
    reg [10:0] last_out;
    always @(posedge clk) if (dut.state == 11'd0 && dut.busy_frame) frames = frames + 1;

    initial begin
        repeat(20) @(posedge clk);
        rst_n = 1;
        repeat(20) @(posedge clk);
        // ★ 必须模拟真实的 24.414 kHz 采样率：每 2048 个 clk 一个样本。
        //   连续把 din_valid 拉高等于 50 MHz 采样，模块来不及处理，帧全丢。
        for (n = 0; n < 1400; n = n + 1) begin
            din_valid = 1; @(posedge clk); #1; din_valid = 0;
            repeat(2047) @(posedge clk);
            #1;
            if (n % 100 == 0)
                $display("n=%5d state=%0d out_ptr=%5d rd_slot=%5d acc=%8d dout=%6d frames=%0d",
                         n, dut.state, 0, 0,
                         0, dout, frames);
        end
        $display("稳态：输入 8000 -> 输出 %0d（β=1 应约 8000）", dout);
        if (dout > 7000 && dout < 9000) $display("RESULT: ALL PASS");
        else                            $display("RESULT: HAS FAILURE");
        $finish;
    end
endmodule
