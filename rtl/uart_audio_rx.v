// -----------------------------------------------------------------------------
//  uart_audio_rx.v
//
//  PC -> FPGA audio over the ACX720 onboard CH340 UART (FPGA_UART_RX = J21).
//
//  Frame (fixed 64 stereo samples, little-endian):
//      A5 5A | seq | L0lo L0hi R0lo R0hi | ... | L63.. R63.. | crc_lo crc_hi
//      CRC16-CCITT (init FFFF, poly 1021) over seq + 256 payload bytes.
//
//  UART: 1,500,000 8N1.  Sample rate: 50 MHz / 2048 = 24414.0625 Hz.
//  The parser writes a complete CRC-checked frame into a 1024-entry FIFO.
//  A sample tick pops one stereo sample every 2048 clocks.
//
//  If no valid frame arrives for 100 ms, `active` drops and the top-level
//  mux falls back to the ES8388 codec ADC.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module uart_audio_rx #(
    parameter integer CLK_HZ  = 50_000_000,
    parameter integer BAUD    = 1_500_000,
    parameter integer FS_HZ   = 24_414,
    parameter integer N       = 64,
    parameter integer FIFO_AW = 10
)(
    input  wire        clk,
    input  wire        rst_n,
    input  wire        uart_rx,

    output reg  [15:0] left,
    output reg  [15:0] right,
    output reg         valid,       // one clk pulse per 24.414 kHz sample
    output reg         active,      // a valid frame has arrived recently

    output reg  [15:0] frame_good,
    output reg  [15:0] crc_err,
    output reg  [15:0] fifo_ovf,
    output reg  [15:0] fifo_unf,
    output reg         frame_pulse,  // one clk pulse when a good frame is parsed
    // ★ 把收到的字节**并联**出来，给 cfg_parser 用。不新增接收器：
    //   一条 UART 只能有一个采样电路，两个接收器会各自对齐到不同的起始位。
    output wire [7:0]  mon_byte,
    output wire        mon_valid
);

    // ---------------- UART byte receiver ----------------
    wire [7:0] rx_byte;
    wire       rx_valid;
    assign mon_byte  = rx_byte;
    assign mon_valid = rx_valid;

    uart_rx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) u_rx (
        .clk   (clk),
        .rst_n (rst_n),
        .rx    (uart_rx),
        .data  (rx_byte),
        .valid (rx_valid)
    );

    // ---------------- CRC16-CCITT ----------------
    function [15:0] crc16;
        input [15:0] c;
        input [7:0]  b;
        integer i;
        reg [15:0] x;
        begin
            x = c ^ {b, 8'h00};
            for (i = 0; i < 8; i = i + 1)
                x = x[15] ? ((x << 1) ^ 16'h1021) : (x << 1);
            crc16 = x;
        end
    endfunction

    // ---------------- frame parser ----------------
    localparam [2:0] S_H1 = 3'd0, S_H2 = 3'd1, S_SEQ = 3'd2,
                     S_DATA = 3'd3, S_CRCL = 3'd4, S_CRCH = 3'd5;

    reg [2:0]  state;
    reg [7:0]  byte_idx;
    reg [15:0] crc;
    reg [7:0]  crc_lo;
    reg [7:0]  l_lo, r_lo;
    reg [15:0] l_tmp;
    reg [15:0] st_l [0:N-1];
    reg [15:0] st_r [0:N-1];

    // commit engine
    reg        commit;
    reg [6:0]  cidx;

    // FIFO (packed {right,left})
    reg [31:0] fifo [0:(1<<FIFO_AW)-1];
    reg [FIFO_AW-1:0] wr_ptr, rd_ptr;
    reg [FIFO_AW:0]   count;
    wire fifo_full  = (count == (1<<FIFO_AW));
    wire fifo_empty = (count == 0);

    // sample tick
    localparam integer TICK_DIV = CLK_HZ / FS_HZ;   // 2048
    reg [11:0] tick_cnt;
    wire sample_tick = (tick_cnt == TICK_DIV - 1);

    // 100 ms timeout
    localparam integer TIMEOUT = CLK_HZ / 10;
    reg [22:0] idle_cnt;

    wire do_write = commit && !fifo_full;
    wire do_read  = sample_tick && !fifo_empty;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= S_H1;
            byte_idx   <= 8'd0;
            crc        <= 16'hFFFF;
            crc_lo     <= 8'd0;
            l_lo       <= 8'd0;
            r_lo       <= 8'd0;
            l_tmp      <= 16'd0;
            commit     <= 1'b0;
            cidx       <= 7'd0;
            wr_ptr     <= {FIFO_AW{1'b0}};
            rd_ptr     <= {FIFO_AW{1'b0}};
            count      <= {(FIFO_AW+1){1'b0}};
            tick_cnt   <= 12'd0;
            idle_cnt   <= 23'd0;
            left       <= 16'd0;
            right      <= 16'd0;
            valid      <= 1'b0;
            active     <= 1'b0;
            frame_good <= 16'd0;
            crc_err    <= 16'd0;
            fifo_ovf   <= 16'd0;
            fifo_unf   <= 16'd0;
            frame_pulse <= 1'b0;
        end else begin
            valid <= 1'b0;
            frame_pulse <= 1'b0;

            // ---- UART frame state machine ----
            if (rx_valid) begin
                case (state)
                    S_H1: begin
                        if (rx_byte == 8'hA5) state <= S_H2;
                    end
                    S_H2: begin
                        if (rx_byte == 8'h5A)      state <= S_SEQ;
                        else if (rx_byte != 8'hA5) state <= S_H1;
                    end
                    S_SEQ: begin
                        crc      <= crc16(16'hFFFF, rx_byte);
                        byte_idx <= 8'd0;
                        state    <= S_DATA;
                    end
                    S_DATA: begin
                        crc <= crc16(crc, rx_byte);
                        case (byte_idx[1:0])
                            2'd0: l_lo <= rx_byte;
                            2'd1: l_tmp <= {rx_byte, l_lo};
                            2'd2: r_lo <= rx_byte;
                            2'd3: begin
                                st_l[byte_idx[7:2]] <= l_tmp;
                                st_r[byte_idx[7:2]] <= {rx_byte, r_lo};
                            end
                        endcase
                        if (byte_idx == 8'd255) begin
                            state <= S_CRCL;
                        end else begin
                            byte_idx <= byte_idx + 8'd1;
                        end
                    end
                    S_CRCL: begin
                        crc_lo <= rx_byte;
                        state  <= S_CRCH;
                    end
                    S_CRCH: begin
                        if ({rx_byte, crc_lo} == crc) begin
                            commit     <= 1'b1;
                            cidx       <= 7'd0;
                            active     <= 1'b1;
                            idle_cnt   <= 23'd0;
                            frame_good <= frame_good + 16'd1;
                            frame_pulse <= 1'b1;
                        end else begin
                            crc_err <= crc_err + 16'd1;
                        end
                        state <= S_H1;
                    end
                    default: state <= S_H1;
                endcase
            end

            // ---- commit 64 samples into FIFO ----
            if (commit) begin
                if (!fifo_full) begin
                    fifo[wr_ptr] <= {st_r[cidx], st_l[cidx]};
                    wr_ptr <= wr_ptr + 1'b1;
                end else begin
                    fifo_ovf <= fifo_ovf + 16'd1;
                end
                if (cidx == N - 1) commit <= 1'b0;
                else               cidx   <= cidx + 7'd1;
            end

            // ---- 24.414 kHz sample tick ----
            if (sample_tick) begin
                tick_cnt <= 12'd0;
                if (!fifo_empty) begin
                    {right, left} <= fifo[rd_ptr];
                    rd_ptr <= rd_ptr + 1'b1;
                    valid  <= 1'b1;
                end else begin
                    // hold last sample; keep the pipeline running
                    valid     <= 1'b1;
                    fifo_unf  <= fifo_unf + 16'd1;
                end
            end else begin
                tick_cnt <= tick_cnt + 12'd1;
            end

            // ---- FIFO count ----
            count <= count + (do_write ? 1'b1 : 1'b0) - (do_read ? 1'b1 : 1'b0);

            // ---- activity timeout ----
            if (active) begin
                if (idle_cnt >= TIMEOUT - 1) begin
                    active   <= 1'b0;
                    idle_cnt <= 23'd0;
                end else begin
                    idle_cnt <= idle_cnt + 23'd1;
                end
            end
        end
    end

endmodule
