`timescale 1ns / 1ps

module tb_uart_audio_rx;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    reg uart_rx = 1'b1;

    wire [15:0] left, right;
    wire        valid, active;
    wire [15:0] fg, ce, fo, fu;

    always #10 clk = ~clk;   // 50 MHz

    uart_audio_rx dut (
        .clk(clk), .rst_n(rst_n), .uart_rx(uart_rx),
        .left(left), .right(right), .valid(valid), .active(active),
        .frame_good(fg), .crc_err(ce), .fifo_ovf(fo), .fifo_unf(fu)
    );

    function [15:0] crc16;
        input [15:0] c;
        input [7:0] b;
        integer i;
        reg [15:0] x;
        begin
            x = c ^ {b, 8'h00};
            for (i = 0; i < 8; i = i + 1)
                x = x[15] ? ((x << 1) ^ 16'h1021) : (x << 1);
            crc16 = x;
        end
    endfunction

    task send_byte(input [7:0] b);
        integer i;
        begin
            uart_rx = 1'b0; #666.667;
            for (i = 0; i < 8; i = i + 1) begin
                uart_rx = b[i]; #666.667;
            end
            uart_rx = 1'b1; #666.667;
        end
    endtask

    integer i, nvalid;
    reg [7:0] payload [0:255];
    reg [15:0] crc;
    reg [15:0] first_l, first_r;

    initial begin
        // build 64 stereo samples: L=1000+i, R=-1000-i
        for (i = 0; i < 64; i = i + 1) begin
            payload[4*i+0] = (1000 + i) & 8'hFF;
            payload[4*i+1] = ((1000 + i) >> 8) & 8'hFF;
            payload[4*i+2] = (-1000 - i) & 8'hFF;
            payload[4*i+3] = ((-1000 - i) >> 8) & 8'hFF;
        end

        crc = 16'hFFFF;
        crc = crc16(crc, 8'd7);
        for (i = 0; i < 256; i = i + 1)
            crc = crc16(crc, payload[i]);

        #200;
        rst_n = 1'b1;
        #200;

        send_byte(8'hA5);
        send_byte(8'h5A);
        send_byte(8'd7);
        for (i = 0; i < 256; i = i + 1)
            send_byte(payload[i]);
        send_byte(crc[7:0]);
        send_byte(crc[15:8]);

        #100;
        nvalid = 0;
        first_l = 16'hxxxx;
        first_r = 16'hxxxx;
        repeat (140000) begin
            @(posedge clk);
            if (valid) begin
                if (nvalid == 0) begin
                    first_l = left;
                    first_r = right;
                end
                nvalid = nvalid + 1;
            end
        end

        $display("ACTIVE=%b GOOD=%0d CRCERR=%0d VALID=%0d FIRST_L=%0d FIRST_R=%0d",
                 active, fg, ce, nvalid, $signed(first_l), $signed(first_r));
        if (active !== 1'b1) $display("FAIL: not active");
        else if (nvalid < 60) $display("FAIL: too few valid");
        else if ($signed(first_l) !== 1000 || $signed(first_r) !== -1000)
            $display("FAIL: first sample mismatch");
        else $display("PASS");
        $finish;
    end

endmodule
