// ============================================================================
//  tb_age_core.v    age_core 冒烟测试台：把输出吐成十进制文本，
//                   由 tool/chk_age_core.py 用 Python 判（复用 sim_age 的测量）。
//
//  为什么不在 Verilog 里判：共振峰位置、包络缩放比这类判据在 RTL 里写不出来
//  （要插值、要搜索），而 Python 那套 est_beta 已经在合成元音上验过（负对照
//  β̂=1.000）。让 RTL 只负责"把数吐出来"，判定交给验过的代码。
// ============================================================================
`timescale 1ns / 1ps

module tb_age_core;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    always #10 clk = ~clk;                  // 50 MHz

    localparam integer CLKS_PER_SAMP = 2048;   // 50e6 / 24414

    reg  [15:0] beta16 = 16'd16384;         // Q1.14，16384 = 1.0
    reg         en = 1'b1;
    reg signed [15:0] din = 16'sd0;
    reg         din_valid = 1'b0;
    wire signed [15:0] dout;
    wire        dout_valid;

    age_core dut (
        .clk(clk), .rst_n(rst_n), .en(en),
        .beta16(beta16),
        .din(din), .din_valid(din_valid),
        .dout(dout), .dout_valid(dout_valid)
    );

    integer fout, fin, i, j;
    real sr, f0, t;

    localparam integer NS = 2600;           // 样本数（约 0.2 s @24.414 kHz）

    initial begin
        // beta 可由 plusarg 覆盖：xsim ... -testplusarg BETA=20480
        begin : blk_beta
            reg [31:0] v;
            if ($value$plusargs("BETA=%d", v)) beta16 = v[15:0];
        end
        sr = 24414.0625;
        fout = $fopen("age_core_out.txt", "w");
        fin  = $fopen("age_core_in.txt",  "w");
        repeat (20) @(posedge clk);
        rst_n = 1'b1;
        repeat (20) @(posedge clk);

        for (i = 0; i < NS; i = i + 1) begin : blk_loop
            // 谐波丰富的"元音"：F0=150 Hz，共振峰 700/1200/2600
            real acc;
            begin : gen
                integer h;
                real a, fh;
                acc = 0.0;
                for (h = 1; h <= 60; h = h + 1) begin
                    fh = 150.0 * h;
                    if (fh > 9000.0) h = 999;
                    else begin
                        a = 0.0;
                        a = a + 1.0 / (1.0 + ((fh - 700.0) / 195.0) * ((fh - 700.0) / 195.0));
                        a = a + 1.0 / (1.0 + ((fh - 1200.0) / 240.0) * ((fh - 1200.0) / 240.0));
                        a = a + 0.6 / (1.0 + ((fh - 2600.0) / 375.0) * ((fh - 2600.0) / 375.0));
                        acc = acc + a * $sin(2.0 * 3.14159265358979 * fh * i / sr);
                    end
                end
            end
            din = $rtoi(acc / 2.2 * 16384.0);   // 归一化到约 ±0.5
            if (din >  32767) din =  32767;
            if (din < -32768) din = -32768;

            din_valid = 1'b1;
            @(posedge clk);
            din_valid = 1'b0;
            $fwrite(fin,  "%0d\n", din);
            if (dout_valid) $fwrite(fout, "%0d\n", dout);
            for (j = 0; j < CLKS_PER_SAMP - 1; j = j + 1) @(posedge clk);
        end
        $fclose(fin);
        $fclose(fout);
        $display("TB done: %0d samples, beta16=%0d", NS, beta16);
        $finish;
    end

endmodule
