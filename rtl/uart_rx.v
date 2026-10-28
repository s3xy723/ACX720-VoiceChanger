// -----------------------------------------------------------------------------
//  uart_rx.v  --  simple 8N1 UART receiver (16x-style mid-bit sampling)
//
//  CLK_HZ / BAUD are integers; DIV is rounded. At 50 MHz / 1.5 Mbaud DIV=33,
//  which is 1.0% fast -- well inside UART tolerance.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module uart_rx #(
    parameter integer CLK_HZ = 50_000_000,
    parameter integer BAUD    = 1_500_000
)(
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rx,
    output reg  [7:0] data,
    output reg        valid
);

    localparam integer DIV = (CLK_HZ + (BAUD / 2)) / BAUD;

    reg [1:0]  sync;
    reg [15:0] cnt;
    reg [3:0]  bitidx;          // 0=start, 1..8=data, 9=stop
    reg [7:0]  sh;
    reg        busy;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sync   <= 2'b11;
            cnt    <= 16'd0;
            bitidx <= 4'd0;
            sh     <= 8'd0;
            busy   <= 1'b0;
            data   <= 8'd0;
            valid  <= 1'b0;
        end else begin
            sync  <= {sync[0], rx};
            valid <= 1'b0;

            if (!busy) begin
                // start bit: line goes low
                if (sync[1] == 1'b0) begin
                    busy   <= 1'b1;
                    bitidx <= 4'd0;
                    cnt    <= (DIV / 2);       // sample middle of start bit
                end
            end else begin
                if (cnt == 16'd0) begin
                    if (bitidx == 4'd0) begin
                        if (sync[1] == 1'b0) begin
                            bitidx <= 4'd1;
                            cnt    <= DIV - 1;
                        end else begin
                            busy   <= 1'b0;    // false start
                        end
                    end else if (bitidx <= 4'd8) begin
                        sh[bitidx - 4'd1] <= sync[1];
                        bitidx <= bitidx + 4'd1;
                        cnt    <= DIV - 1;
                    end else begin             // stop bit
                        if (sync[1] == 1'b1) begin
                            data  <= sh;
                            valid <= 1'b1;
                        end
                        busy <= 1'b0;
                    end
                end else begin
                    cnt <= cnt - 16'd1;
                end
            end
        end
    end

endmodule
