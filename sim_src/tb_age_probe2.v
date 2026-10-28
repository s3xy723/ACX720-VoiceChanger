`timescale 1ns / 1ps
// 直流输入下逐 bin 打印 age_core 在写回阶段"读到什么、算出什么"。
// 直流信号理论上只有 bin0 非零；如果 bin1/2/3 也很大，说明读地址和
// 数据没对齐（读延迟 3 拍没等够）。
module tb_age_probe2;
    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;

    reg [15:0] beta16 = 16'd16384;         // β = 1.0
    reg signed [15:0] din = 0;
    reg din_valid = 0;
    wire signed [15:0] dout;
    wire dout_valid;

    age_core dut(.clk(clk), .rst_n(rst_n), .en(1'b1), .beta16(beta16),
                 .din(din), .din_valid(din_valid), .dout(dout), .dout_valid(dout_valid));

    integer i, j, n;
    initial begin
        repeat(20) @(posedge clk); rst_n = 1; repeat(20) @(posedge clk);
        din_valid = 1;
        for (n = 0; n < 900; n = n + 1) begin
            din = 16'sd8000;
            @(posedge clk);
            for (j = 0; j < 2047; j = j + 1) @(posedge clk);   // 每样本给 2048 拍，够整帧处理
            #1;
            // 写回 Y 的那一拍：把 idx / 读到的 X / 增益商 打出来
            if (dut.f_wr && (dut.idx < 11'd6 || dut.idx == 11'd256))
                $display("  WRAP idx=%0d xre=%0d xim=%0d e=%0d e2=%0d divq=%0d",
                         dut.idx, dut.xre_r, dut.xim_r, dut.e_r, dut.e2_r, dut.div_q);
            if (n % 100 == 0)
                $display("n=%4d state=%0d out_ptr=%4d rd_slot=%4d acc[rd_slot]=%0d dout=%0d",
                         n, dut.state, dut.out_ptr, dut.rd_slot, dut.acc[dut.rd_slot], dout);
        end
        din_valid = 0;
        repeat(200) @(posedge clk);
        $display("DC 稳态输出 dout=%0d (输入 8000, β=1 应约 8000)", dout);
        $finish;
    end
endmodule
