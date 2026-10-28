// ============================================================================
//  tb_top.v   端到端功能仿真
//
//  本 TB 同时提供三样东西：
//    1. 一个 I2S 从机模型，按 FPGA 产生的 BCLK/LRCLK 把正弦样本串行喂给 ADCDAT
//    2. 一个 I2S 接收解码器，把 DACDAT 还原成并行样本写进文件
//    3. 一个 I2C 监听器，把 WM8731 配置写入的寄存器解出来打印
//
//  TB 里复现了一份和 i2s_if 完全同源的 0..1023 时基计数器，这样位槽编号
//  必然对齐，不需要去猜 cnt。仿真结束后用 tool/analyze_sim.py 做频谱分析。
//
//  时钟周期 20 ns（50 MHz），单帧 1024 个周期 = 20.48 us。
//  每个阶段的帧数由目录下的 sim_frames.txt 决定（首行一个十进制整数）。
//  文件不存在时默认 4096 帧（约 84 ms，足够频谱分析用）。
//  之所以不用 $value$plusargs：Vivado 2018.3 的 xsim 不认 --testplusarg，
//  会报 "Expected a switch but found 2"。
// ============================================================================
`timescale 1ns / 1ps

module tb_top;

    integer frames;

    initial begin
        begin : read_cfg
            integer fh, stat;
            fh = $fopen("sim_frames.txt", "r");
            if (fh != 0) begin
                stat = $fscanf(fh, "%d", frames);
                $fclose(fh);
                if (stat != 1) frames = 4096;
            end else begin
                frames = 4096;
            end
        end
        $display("FRAMES per phase = %0d", frames);
    end

    reg         clk = 1'b0;
    reg         reset_n = 1'b0;
    reg  [7:0]  sw = 8'h00;

    wire        i2c_sclk;
    wire        i2c_sdat;
    wire        aud_mclk, aud_bclk, aud_lrclk, aud_dacdat;
    wire        aud_adcdat;
    wire [7:0]  led;

    reg         adcdat_reg = 1'b0;
    assign aud_adcdat = adcdat_reg;

    pullup (i2c_sdat);                 // 模拟板上 4.7k 上拉

    voice_changer_top dut (
        .clk        (clk),
        .reset_n    (reset_n),
        .sw         (sw),
        .led        (led),
        .i2c_sclk   (i2c_sclk),
        .i2c_sdat   (i2c_sdat),
        .aud_mclk   (aud_mclk),
        .aud_bclk   (aud_bclk),
        .aud_lrclk  (aud_lrclk),
        .aud_dacdat (aud_dacdat),
        .aud_adcdat (aud_adcdat)
    );

    always #10 clk = ~clk;

    // ------------------------------------------------------------------
    //  与 i2s_if 同源的时基
    // ------------------------------------------------------------------
    reg [9:0] cnt;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) cnt <= 10'd0;
        else          cnt <= cnt + 10'd1;
    end

    wire [4:0] slot = cnt[8:4];
    wire [3:0] bph  = cnt[3:0];

    // ------------------------------------------------------------------
    //  激励：两个声道的正弦
    //  用一个独立相位累加器，频率 = FS * FSTEP / 65536
    //      440 Hz  -> 440*65536/48828.125 = 591
    // ------------------------------------------------------------------
    localparam integer FSTEP = 591;    // ~440 Hz

    reg [15:0] ph;
    reg signed [15:0] samp_l, samp_r;

    // Verilog-2001 不允许在 function 内部声明 real，必须提到 module 层
    real sin_arg;

    function signed [15:0] sine16;
        input [15:0] phase;
        begin
            sin_arg = 6.28318530718 * phase / 65536.0;
            sine16  = $rtoi(19660.0 * $sin(sin_arg));   // 0.6 * 32767
        end
    endfunction

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            ph     <= 16'd0;
            samp_l <= 16'sd0;
            samp_r <= 16'sd0;
        end else if (cnt == 10'd1000) begin
            ph     <= ph + FSTEP;
            samp_l <= sine16(ph);
            samp_r <= sine16(ph);
        end
    end

    // ------------------------------------------------------------------
    //  I2S 从机 -> FPGA（ADCDAT）
    //  和 DUT 发送端同样的装填方式：slot0 输出 0，slot1..16 输出 MSB..LSB
    // ------------------------------------------------------------------
    reg [15:0] sr_tx;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            sr_tx      <= 16'd0;
            adcdat_reg <= 1'b0;
        end else if (bph == 4'd0) begin
            if (cnt == 10'd0) begin
                sr_tx      <= samp_l;
                adcdat_reg <= 1'b0;
            end else if (cnt == 10'd512) begin
                sr_tx      <= samp_r;
                adcdat_reg <= 1'b0;
            end else begin
                sr_tx      <= {sr_tx[14:0], 1'b0};
                adcdat_reg <= sr_tx[15];
            end
        end
    end

    // ------------------------------------------------------------------
    //  FPGA -> I2S 从机（DACDAT）捕获
    // ------------------------------------------------------------------
    reg [15:0] sr_rx;
    integer    dac_file;
    integer    out_cnt;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            sr_rx <= 16'd0;
        end else begin
            // DUT 在 bph==0 更新 DACDAT，故在 bph==12 采样最稳
            if ((bph == 4'd12) && (slot >= 5'd1) && (slot <= 5'd16))
                sr_rx <= {sr_rx[14:0], aud_dacdat};

            if (cnt == 10'd272 && dac_file != 0) begin   // 左声道帧结束时
                $fwrite(dac_file, "%0d\n", $signed(sr_rx));
                out_cnt <= out_cnt + 1;
            end
        end
    end

    // ------------------------------------------------------------------
    //  I2C 监听器：把每次写入的 {addr,value} 打印出来
    // ------------------------------------------------------------------
    reg [7:0]  ic_shift;
    reg [4:0]  ic_bit;
    reg [2:0]  ic_byte;
    reg        ic_scl_d;
    reg        ic_started;
    reg        ic_ack_ph;      // 下一个 scl_rise 是应答位，不计入数据
    reg [7:0]  ic_b0, ic_b1, ic_b2;

    wire ic_scl_rise =  i2c_sclk & ~ic_scl_d;
    wire ic_scl_fall = ~i2c_sclk &  ic_scl_d;
    reg  ic_sda_d;

    always @(posedge clk) begin
        ic_scl_d <= i2c_sclk;
        ic_sda_d <= i2c_sdat;
    end

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            ic_bit     <= 5'd0;
            ic_byte    <= 3'd0;
            ic_started <= 1'b0;
        end else begin
            // START：SCL 高时 SDA 下降沿
            if (i2c_sclk && ic_sda_d && !i2c_sdat) begin
                ic_started <= 1'b1;
                ic_bit     <= 5'd0;
                ic_byte    <= 3'd0;
                ic_ack_ph  <= 1'b0;
                $write("[%0t] I2C START ", $time);
            end
            // STOP：SCL 高时 SDA 上升沿
            else if (i2c_sclk && !ic_sda_d && i2c_sdat) begin
                if (ic_started) begin
                    // WM8731 两线格式: byte1 = {addr[6:0], value[8]}
                    //                 byte2 = value[7:0]
                    $display("-> STOP  dev=0x%02h reg=0x%02h val=0x%03h",
                             ic_b0[7:1], ic_b1[7:1], {ic_b1[0], ic_b2});
                end
                ic_started <= 1'b0;
            end
            else if (ic_started && ic_scl_rise) begin
                // 应答位必须跳过：每个字节后面跟着一个 scl 高脉冲，
                // 把它计入位计数器会让之后每个字节都整体错位一位。
                if (ic_ack_ph) begin
                    ic_ack_ph <= 1'b0;
                    ic_bit    <= 5'd0;
                end else begin
                    ic_shift <= {ic_shift[6:0], i2c_sdat};
                    if (ic_bit == 5'd7) begin
                        case (ic_byte)
                            3'd0: ic_b0 <= {ic_shift[6:0], i2c_sdat};
                            3'd1: ic_b1 <= {ic_shift[6:0], i2c_sdat};
                            3'd2: ic_b2 <= {ic_shift[6:0], i2c_sdat};
                        endcase
                        ic_bit    <= 5'd0;
                        ic_byte   <= ic_byte + 3'd1;
                        ic_ack_ph <= 1'b1;
                    end else begin
                        ic_bit <= ic_bit + 5'd1;
                    end
                end
            end
        end
    end

    // ------------------------------------------------------------------
    //  主流程：三段 "", 各 FRAMES 帧
    //    阶段 0 : sw=00  直通      期望输出 440 Hz
    //    阶段 1 : sw=9C  变调 r=1.0（v=7）
    //    阶段 2 : sw=BC  变调 r=2.0（v=15）
    //    阶段 3 : sw=8C  变调 r=0.5（v=3）
    //    阶段 4 : sw=41  环形调制 100 Hz
    // ------------------------------------------------------------------
    integer phase_i;
    reg [7:0] sw_tab [0:4];
    initial begin
        sw_tab[0] = 8'h00;
        sw_tab[1] = 8'h9C;
        sw_tab[2] = 8'hBC;
        sw_tab[3] = 8'h8C;
        sw_tab[4] = 8'h41;
    end

    task do_phase;
        input integer idx;
        input [8*24:1] name;
        begin
            dac_file = $fopen({"sim_out_", name, ".txt"}, "w");
            sw       = sw_tab[idx];
            out_cnt  = 0;
            $display("---- phase %0d  sw=0x%02h  file=sim_out_%0s.txt ----",
                     idx, sw, name);
            repeat (frames * 1024) @(posedge clk);
            $fclose(dac_file);
            dac_file = 0;
        end
    endtask

    initial begin
        dac_file = 0;
        out_cnt  = 0;
        reset_n  = 1'b0;
        repeat (20) @(posedge clk);
        reset_n  = 1'b1;
        $display("reset released at %0t", $time);
        // 等 codec 配置跑完（10 ms 上电等待 + 11 个寄存器 * 29 * 4 * 125 * 20ns）
        wait (led[2] == 1'b1);
        $display("codec config done at %0t", $time);

        do_phase(0, "00_bypass");
        do_phase(1, "01_pitch_r1");
        do_phase(2, "02_pitch_r2");
        do_phase(3, "03_pitch_r0p5");
        do_phase(4, "04_ringmod");

        $display("SIM DONE");
        $finish;
    end

    initial begin
        // 兜底：防止卡死
        #1_000_000_000;
        $display("TIMEOUT: simulation was killed");
        $finish;
    end

endmodule
