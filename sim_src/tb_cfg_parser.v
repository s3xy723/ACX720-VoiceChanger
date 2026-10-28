`timescale 1ns / 1ps
// cfg_parser 行为验收：
//   P1 合法包 -> 参数生效，pkt_ok 加一，o_pulse 打一拍
//   P2 CRC 错 -> 参数**不许变**，pkt_err 加一（负对照）
//   P3 音频帧头 A5 5A 的字节流不许被当成配置（不许误触发、不许记错）
//   P4 AA AA 55 这种重复魔数要能重新同步
module tb_cfg_parser;
    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;

    reg [7:0] rx_byte = 0;
    reg       rx_valid = 0;
    wire signed [15:0] beta16, dphase;
    wire signed [7:0]  tilt;
    wire [7:0] breath, shim;
    wire en, pulse;
    wire [15:0] ok, err;

    cfg_parser dut(.clk(clk), .rst_n(rst_n), .rx_byte(rx_byte), .rx_valid(rx_valid),
                   .o_beta16(beta16), .o_dphase(dphase), .o_coef_tilt(tilt),
                   .o_coef_breath(breath), .o_coef_shim(shim), .o_en(en),
                   .o_pulse(pulse), .o_pkt_ok(ok), .o_pkt_err(err));

    integer i, bad = 0;

    function [15:0] crc16f;
        input [15:0] c;
        input [7:0]  b;
        integer k;
        reg [15:0] x;
        begin
            x = c ^ {b, 8'h00};
            for (k = 0; k < 8; k = k + 1)
                x = x[15] ? ((x << 1) ^ 16'h1021) : (x << 1);
            crc16f = x;
        end
    endfunction

    reg [7:0] pl [0:9];
    reg [15:0] c;

    task send;
        input [7:0] b;
        begin
            rx_byte = b; rx_valid = 1; @(posedge clk); rx_valid = 0;
            repeat(3) @(posedge clk);
        end
    endtask

    task send_pkt;
        input integer corrupt;
        integer k;
        begin
            send(8'hAA); send(8'h55); send(8'h01); send(8'd10);
            c = 16'hFFFF; c = crc16f(c, 8'h01); c = crc16f(c, 8'd10);
            for (k = 0; k < 10; k = k + 1) begin
                send(pl[k]);
                c = crc16f(c, pl[k]);
            end
            if (corrupt) c = c ^ 16'h0001;      // 故意打坏一位
            $display("CRC payload=[0,80,0,0,250,20,30,1,0,0] -> %04h", c);
            send(c[7:0]); send(c[15:8]);
            repeat(10) @(posedge clk);
        end
    endtask

    initial begin
        repeat(8) @(posedge clk); rst_n = 1; repeat(8) @(posedge clk);

        // ---- P1 合法包：beta=1.25(20480), dphase=0, tilt=-6, breath=20, shim=30, en=1
        pl[0]=8'd0; pl[1]=8'd80;          // 20480 = 0x5000
        pl[2]=8'd0;  pl[3]=8'd0;
        pl[4]=8'hFA;                       // -6
        pl[5]=8'd20; pl[6]=8'd30; pl[7]=8'd1;
        pl[8]=8'd0;  pl[9]=8'd0;
        send_pkt(0);
        $display("P1 beta=%0d dphase=%0d tilt=%0d breath=%0d shim=%0d en=%b ok=%0d err=%0d",
                 beta16, dphase, tilt, breath, shim, en, ok, err);
        if (beta16 != 16'sd20480 || en !== 1'b1 || ok != 16'd1 || err != 16'd0) bad = bad + 1;

        // ---- P2 CRC 错：参数不许变
        pl[1]=8'd1;                        // 想改成 256
        send_pkt(1);
        $display("P2 beta=%0d（必须仍是 20480） ok=%0d err=%0d", beta16, ok, err);
        if (beta16 != 16'sd20480 || err != 16'd1 || ok != 16'd1) bad = bad + 1;

        // ---- P3 音频帧头：不许被当成配置
        send(8'hA5); send(8'h5A); send(8'h01); send(8'd10);
        for (i = 0; i < 12; i = i + 1) send(8'h3C);
        $display("P3 beta=%0d ok=%0d err=%0d（都不许变）", beta16, ok, err);
        if (beta16 != 16'sd20480 || ok != 16'd1 || err != 16'd1) bad = bad + 1;

        // ---- P4 AA AA 55 重同步
        send(8'hAA); send(8'hAA); send(8'h55); send(8'h01); send(8'd10);
        c = 16'hFFFF; c = crc16f(c, 8'h01); c = crc16f(c, 8'd10);
        pl[0]=8'd0; pl[1]=8'd64;           // 16384 = 1.0
        pl[4]=8'd0; pl[5]=8'd0; pl[6]=8'd0; pl[7]=8'd0;
        for (i = 0; i < 10; i = i + 1) begin
            send(pl[i]); c = crc16f(c, pl[i]);
        end
        send(c[7:0]); send(c[15:8]);
        repeat(10) @(posedge clk);
        $display("P4 beta=%0d en=%b ok=%0d", beta16, en, ok);
        if (beta16 != 16'sd16384 || en !== 1'b0 || ok != 16'd2) bad = bad + 1;

        if (bad == 0) $display("RESULT: ALL PASS");
        else          $display("RESULT: HAS FAILURE (%0d)", bad);
        $finish;
    end
endmodule
