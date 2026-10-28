// Simple 8N1 UART transmitter, 50 MHz / 1.5 Mbaud (DIV=33).
`timescale 1ns / 1ps
module uart_tx #(
    parameter integer CLK_HZ = 50_000_000,
    parameter integer BAUD   = 1_500_000
)(
    input  wire       clk,
    input  wire       rst_n,
    input  wire       send,
    input  wire [7:0] data,
    output reg        tx
);
    localparam integer DIV = (CLK_HZ + (BAUD / 2)) / BAUD;
    reg [15:0] cnt;
    reg [3:0]  bitidx;
    reg [7:0]  sh;
    reg        busy;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            tx <= 1'b1; cnt <= 16'd0; bitidx <= 4'd0; sh <= 8'd0; busy <= 1'b0;
        end else if (!busy) begin
            tx <= 1'b1;
            if (send) begin
                busy <= 1'b1; sh <= data; bitidx <= 4'd0; cnt <= DIV - 1;
                tx <= 1'b0;                 // start bit
            end
        end else begin
            if (cnt == 16'd0) begin
                cnt <= DIV - 1;
                if (bitidx < 4'd8) begin
                    tx <= sh[bitidx];
                    bitidx <= bitidx + 4'd1;
                end else begin
                    tx <= 1'b1;             // stop bit
                    busy <= 1'b0;
                end
            end else begin
                cnt <= cnt - 16'd1;
            end
        end
    end
endmodule
