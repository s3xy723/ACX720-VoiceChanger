// ============================================================================
//  tb_age_tint.v    age_tint 自检测试台（xsim）
//
//  三条判据，每条都能 FAIL：
//    T1 en=0 必须**逐位直通**（对应"同年龄→同年龄"），且必须是真比过的样本数 > 0
//    T2 en=1 且只开气声时，输出必须与输入不同
//    T3 tilt=+4 与 tilt=-4 的高频含量必须分开（亮 > 暗）
//
//  ★ 第一版这里踩了坑：din_valid 只给一拍、之后等 8 拍再检查，那时
//    dout_valid 早就落了，于是"0 个样本比较、0 个不等"也报 PASS —— 典型的
//    恒真断言。现在改成**连续 valid、逐拍比对**，并且显式打印比较过的样本数。
// ============================================================================
`timescale 1ns / 1ps

module tb_age_tint;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    always #10 clk = ~clk;              // 50 MHz

    reg               en = 1'b0;
    reg  signed [15:0] coef_breath = 16'sd0;
    reg  signed [15:0] coef_shim   = 16'sd0;
    reg  signed [15:0] coef_tilt   = 16'sd0;
    reg  signed [15:0] din = 16'sd0;
    reg                din_valid = 1'b0;
    wire signed [15:0] dout;
    wire               dout_valid;

    age_tint dut (
        .clk(clk), .rst_n(rst_n), .en(en),
        .coef_breath(coef_breath), .coef_shim(coef_shim),
        .coef_tilt(coef_tilt),
        .din(din), .din_valid(din_valid),
        .dout(dout), .dout_valid(dout_valid)
    );

    // 输入的两级延迟（与 DUT 的两级流水对齐）
    reg signed [15:0] r1 = 16'sd0, r2 = 16'sd0, r3 = 16'sd0;
    always @(posedge clk) begin
        if (din_valid) begin r1 <= din; r2 <= r1; r3 <= r2; end
    end

    integer nchk, nmis, prev;
    real    e_diff, hf_acc;     // ★ 64 位以上：800×4e9 会撑爆 32 位 integer

    localparam integer NSAMP = 800;

    // 连续 valid，逐拍驱动；1 kHz + 4.5 kHz 复合正弦（4.5k 用来观察高频倾斜）
    task run_case;
        integer i;
        begin
            nchk = 0; nmis = 0; e_diff = 0.0; hf_acc = 0.0; prev = 0;
            din_valid = 1'b1;
            for (i = 0; i < NSAMP; i = i + 1) begin
                din = $rtoi(12000.0 * ($sin(2.0 * 3.14159265358979 * 1000.0 * i / 24414.0)
                             + 0.7 * $sin(2.0 * 3.14159265358979 * 4500.0 * i / 24414.0)));
                @(posedge clk);
                #1;                       // 避开与非阻塞赋值的竞争
                if (dout_valid) begin
                    nchk   = nchk + 1;
                    if (dout !== r3) nmis = nmis + 1;
                    e_diff = e_diff + 1.0 * (dout - r3) * (dout - r3);
                    hf_acc = hf_acc + 1.0 * (dout - prev) * (dout - prev);
                    prev   = dout;
                end
            end
            din_valid = 1'b0;
        end
    endtask

    integer pass, fail;
    real hf_bright;

    initial begin
        pass = 0; fail = 0;
        repeat (10) @(posedge clk);
        rst_n = 1'b1;
        repeat (10) @(posedge clk);

        // ---- T1 直通
        // ★ 系数必须给满：全 0 时处理路径算出来本来就等于直通，
        //   这条测试就变成恒真了（实测把 en 删掉也照样 PASS）。
        en = 1'b0; coef_breath = 16'sd2048; coef_shim = 16'sd4096; coef_tilt = 16'sd8192;
        run_case;
        if (nchk > NSAMP / 2 && nmis == 0)
            begin pass = pass + 1;
                  $display("  [PASS] T1 en=0 逐位直通（比较了 %0d 个样本，0 个不等）", nchk); end
        else
            begin fail = fail + 1;
                  $display("  [FAIL] T1 直通失败：比较 %0d 个，%0d 个不等", nchk, nmis); end

        // ---- T2 只加气声（Q4.12 = 0.5）
        en = 1'b1; coef_breath = 16'sd2048; coef_shim = 0; coef_tilt = 0;
        run_case;
        if (nchk > NSAMP / 2 && nmis > nchk / 2 && e_diff > 0)
            begin pass = pass + 1;
                  $display("  [PASS] T2 气声生效：%0d/%0d 样本改变，差异能量=%.3e",
                           nmis, nchk, e_diff); end
        else
            begin fail = fail + 1;
                  $display("  [FAIL] T2 气声无效：%0d/%0d 改变，能量=%.3e",
                           nmis, nchk, e_diff); end

        // ---- T3 高频倾斜亮/暗分开
        en = 1'b1; coef_breath = 0; coef_shim = 0; coef_tilt = 16'sd8192;
        run_case;
        hf_bright = hf_acc;
        coef_tilt = -16'sd8192;
        run_case;
        if (nchk > NSAMP / 2 && hf_bright > hf_acc * 2)
            begin pass = pass + 1;
                  $display("  [PASS] T3 倾斜分得开：亮=%.3e 暗=%.3e（比值 %.2f）",
                           hf_bright, hf_acc, hf_bright / (hf_acc + 1.0)); end
        else
            begin fail = fail + 1;
                  $display("  [FAIL] T3 倾斜没分开：亮=%.3e 暗=%.3e", hf_bright, hf_acc); end

        $display("");
        $display("  PASS = %0d  FAIL = %0d", pass, fail);
        if (fail == 0) $display("RESULT: ALL PASS");
        else           $display("RESULT: HAS FAILURE");
        $finish;
    end

endmodule
