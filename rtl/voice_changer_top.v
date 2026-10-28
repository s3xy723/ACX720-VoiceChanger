// ============================================================================
//  voice_changer_top.v   ACX720-V3 (XC7A35T) 实时变声器顶层
//
//  硬件（依据官方管脚分配表 ACX720-V3管脚分配表2024_11_13.xlsx）：
//      codec      ES8388（不是 WM8731！）
//      I2C        SCL=M13  SDA=M16   ES8388 器件地址 8'h20（7 位地址 0x10）
//      MCLK(XCLK) V20  由 FPGA 提供给 codec —— 本工程 50MHz/4 = 12.5 MHz
//      BCLK       U20  codec -> FPGA（ES8388 主模式）
//      LRCK       W20  codec -> FPGA
//      ADCDAT     AA21 codec -> FPGA
//      DACDAT     AA20 FPGA -> codec
//      clk        Y18  50 MHz
//      reset_n    B21  S4 按键
//
//  时钟与采样率：
//      MCLK 12.5 MHz，codec 内部按官方配置表分频：
//          LRCK = MCLK / 512 = 24.414 kHz（实际采样率，比 48k 略低但完全可用）
//          BCLK = MCLK / 8   =  1.5625 MHz = 64 * LRCK
//      为什么不是精确的 8.192/12.288 MHz：板上只有 50 MHz 晶振，50 MHz 经 MMCM
//      无法精确得到 12.288（需要 M/D 比的小数分母含 5^5，超出 7 系列 M 上限），
//      所以直接用 50 MHz 分频。codec 只做整数分频，对 MCLK 精度不敏感。
//
//  资源占用估算（XC7A35T 有 20800 LUT / 90 DSP48E1 / 50 BRAM36K）：
//      LUT ~900 / DSP48E1 ~12 / BRAM36K ~9 / 时钟 1 个（无 PLL、无 MMCM）
//
//  拨码开关定义（sw[7:0]）：
//      sw[7:6]  效果选择
//               00  直通      上电第一步一定要先跑这个
//               01  环形调制  机器人声，sw[2:0] 选频率
//               10  变调      核心功能，sw[5:2] 调音高
//               11  回声      sw[3:0] 调延迟长度，sw[5:4] 调反馈量
//
//      sw[5:2]  变调量 v = 0..15
//               dphase = (v - 5) * 6     变调比 r = 1 + dphase/64
//               v=0  -> r=0.531  最低男声（再低会有可闻的跑调）
//               v=5  -> r=1.000  原声
//               v=8  -> r=1.281  女声化
//               v=11 -> r=1.562
//               v=15 -> r=1.937  接近升八度（花栗鼠）
//               每档间隔 6/64 = 0.094（约 1.6 个半音）
//
//      sw[2:0]  环形调制频率：60/100/150/220/350/500/800/1200 Hz
//      sw[3:0]  回声延迟：taps*256 点，24.414kHz 下即 taps*10.5 ms
//      sw[5:4]  回声增益：0.25 / 0.375 / 0.5 / 0.625
//
//  LED（8 颗，全部有确定含义）：
//      led[1:0]  当前效果模式（同 sw[7:6]）
//      led[2]    I2C 配置跑完（43 个 ES8388 寄存器写完）
//      led[3]    左声道有信号          led[4] 右声道有信号
//      led[5]    左声道削波            led[6] 右声道削波
//      led[7]    左右确实不同（立体声相关性统计，见 rtl/stereo_meter.v）
//      左/右是**分别**统计的：单侧过载、左右装反、两路其实一模一样，都能看出来。
//
//  给单片机的音频出口（GPIO0 扩展口，见 rtl/i2s_mirror_tx.v）：
//      mcu_sclk   = SPI 时钟（≈2.083 MHz，模式 0,0）
//      mcu_csn    = 每字一个低脉冲（下降沿 = STM32 EXTI4 帧同步）
//      mcu_mosi   = 16bit 数据，MSB 先行，先左后右，fs = 24414 Hz
//      单片机（STM32F103）配成 SPI1 从机（16bit、模式 0,0、硬件 NSS）即可收到，
//      与板上的 codec 听同一路音频，互不影响。接线见 mcu/README.md。
// ============================================================================
`timescale 1ns / 1ps

module voice_changer_top (
    input  wire        clk,             // 50 MHz
    input  wire        reset_n,         // 低有效，ACX720 的 S4

    input  wire [7:0]  sw,              // 拨码开关
    output reg  [7:0]  led,             // LED

    // ---- ES8388 控制口（I2C）----
    output wire        i2c_sclk,
    inout  wire        i2c_sdat,

    // ---- ES8388 数字音频口 ----
    output wire        aud_mclk,        // 12.5 MHz，FPGA -> codec
    input  wire        aud_bclk,        // codec -> FPGA
    input  wire        aud_lrclk,       // codec -> FPGA
    output wire        aud_dacdat,      // FPGA -> codec
    input  wire        aud_adcdat,      // codec -> FPGA

    // ---- 给单片机的 SPI 镜像总线（GPIO0 扩展口，见 rtl/spi_mirror_tx.v）----
    // 处理完的音频按 SPI 模式 0,0 / 16bit 先左后右发出，供 STM32 收去驱动 MCP4922
    output wire        mcu_sclk,
    output wire        mcu_csn,
    output wire        mcu_mosi,

    // ---- ACX720 板载 CH340 的 FPGA UART（PC -> FPGA 音频）----
    // 1.5 Mbaud 8N1，帧格式见 rtl/uart_audio_rx.v
    input  wire        uart_rx,
    output wire        uart_tx      // 每收到一帧回一个 0xA5，给上位机做链路自检
);

    parameter AW = 12;                  // 延迟线地址宽度 -> 4096 点

    // ------------------------------------------------------------------
    //  MCLK：50 MHz / 4 = 12.5 MHz
    //  用寄存器翻转而不是 assign cnt[1]，避免把衍生时钟当真实时钟网络用。
    // ------------------------------------------------------------------
    reg mclk_r;
    reg mclk_tog;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            mclk_tog <= 1'b0;
            mclk_r   <= 1'b0;
        end else begin
            mclk_tog <= ~mclk_tog;
            if (!mclk_tog) mclk_r <= ~mclk_r;
        end
    end

    assign aud_mclk = mclk_r;

    // ------------------------------------------------------------------
    //  ES8388 寄存器配置（上电延时后自动跑一遍 43 个寄存器）
    //  这套配置表直接取自官方例程 ch35_CoreCourse_GHRD_I2S_RX，一字未改。
    // ------------------------------------------------------------------
    wire cfg_done;

    ES8388_Init u_cfg (
        .Clk           (clk),
        .Rst_n         (reset_n),
        .I2C_Init_Done (cfg_done),
        .i2c_sdat      (i2c_sdat),
        .i2c_sclk      (i2c_sclk)
    );

    // ------------------------------------------------------------------
    //  参数解码
    // ------------------------------------------------------------------
    wire [1:0] mode = sw[7:6];

    // dphase = (v - 5) * 6,  v = sw[5:2]
    //
    // 为什么是 6 而不是 8：参考模型实测，变调比越低，双抽头 Hann 交叉淡化
    // 的输出频率误差越大（两个抽头相距 1024 点，对低频输入相位不相干，
    // 交叉淡化会产生包络起伏）。实测误差：
    //      r=0.125 -> 17%    r=0.25 -> 7%    r=0.375 -> 3.8%
    //      r=0.5   -> 2.3%   r>=0.625 -> <1.4%
    // 取 r >= 0.53 作为下限，全区间误差 < 2%，听感上不跑调。
    wire signed [8:0] pitch_sel    = {5'b0, sw[5:2]};
    wire signed [8:0] pitch_dphase = (pitch_sel - 9'sd5) * 9'sd6;

    wire [2:0] ring_freq = sw[2:0];
    wire [3:0] echo_taps = sw[3:0];
    wire [1:0] echo_gain = sw[5:4];

    // ------------------------------------------------------------------
    //  I2S 从机
    // ------------------------------------------------------------------
    wire [15:0] rx_left, rx_right;
    wire        rx_strobe;

    wire signed [15:0] tx_left, tx_right;

    i2s_slave u_i2s (
        .clk        (clk),
        .rst_n      (reset_n),
        .aud_bclk   (aud_bclk),
        .aud_lrclk  (aud_lrclk),
        .aud_adcdat (aud_adcdat),
        .aud_dacdat (aud_dacdat),
        .rx_left    (rx_left),
        .rx_right   (rx_right),
        .rx_strobe  (rx_strobe),
        .tx_left    (tx_left),
        .tx_right   (tx_right)
    );

    // ------------------------------------------------------------------
    //  PC -> FPGA 音频（ACX720 板载 CH340，FPGA_UART_RX = J21）
    //  1.5 Mbaud，24.414 kHz 立体声，帧头/序号/CRC 见 uart_audio_rx.v。
    //  收到有效帧后自动切到 UART 输入；100 ms 没有帧则回退到 codec ADC。
    // ------------------------------------------------------------------
    wire [15:0] ua_left, ua_right;
    wire        ua_valid, ua_active;

    wire        ua_frame_pulse;
    wire [7:0]  ua_byte;
    wire        ua_byte_valid;

    uart_audio_rx u_uart_audio (
        .clk        (clk),
        .rst_n      (reset_n),
        .uart_rx    (uart_rx),
        .left       (ua_left),
        .right      (ua_right),
        .valid      (ua_valid),
        .active     (ua_active),
        .frame_good (),
        .crc_err    (),
        .fifo_ovf   (),
        .fifo_unf   (),
        .frame_pulse(ua_frame_pulse),
        .mon_byte   (ua_byte),
        .mon_valid  (ua_byte_valid)
    );

    // 每收到一个 CRC 正确的音频帧，回一个 0xA5；上位机据此确认链路通
    uart_tx u_uart_tx (
        .clk   (clk),
        .rst_n (reset_n),
        .send  (ua_frame_pulse),
        .data  (8'hA5),
        .tx    (uart_tx)
    );

    wire [15:0] src_left  = ua_active ? ua_left  : rx_left;
    wire [15:0] src_right = ua_active ? ua_right : rx_right;
    wire        src_valid = ua_active ? ua_valid : rx_strobe;

    // ------------------------------------------------------------------
    //  给单片机的 I2S 镜像总线：把上面这路处理完的音频原样再送一份出去。
    //  codec 是主机（BCLK/LRCK 由它产生），本模块照抄它的时钟线和
    //  aud_dacdat 的位序列，单片机做 I2S 从机接收即可，不需要 FIFO。
    // ------------------------------------------------------------------
    spi_mirror_tx u_mcu_spi (
        .clk       (clk),
        .rst_n     (reset_n),
        .tx_left   (tx_left),
        .tx_right  (tx_right),
        .mcu_sclk  (mcu_sclk),
        .mcu_csn   (mcu_csn),
        .mcu_mosi  (mcu_mosi)
    );

    // ------------------------------------------------------------------
    //  左右声道各一条效果链
    // ------------------------------------------------------------------
    //  年龄参数（PC -> FPGA）。原来 FPGA **完全不解析配置包**，
    //  界面上的年龄设置进不到硬件里；现在用 AA 55 独立魔数并联在
    //  同一条 UART 字节流上，音频帧（A5 5A）不受任何影响。
    wire signed [15:0] cfg_beta16, cfg_dphase;
    wire signed [7:0]  cfg_tilt;
    wire        [7:0]  cfg_breath, cfg_shim;
    wire               cfg_en, cfg_pulse;

    cfg_parser u_age_cfg (
        .clk           (clk),
        .rst_n         (reset_n),
        .rx_byte       (ua_byte),
        .rx_valid      (ua_byte_valid),
        .o_beta16      (cfg_beta16),
        .o_dphase      (cfg_dphase),
        .o_coef_tilt   (cfg_tilt),
        .o_coef_breath (cfg_breath),
        .o_coef_shim   (cfg_shim),
        .o_en          (cfg_en),
        .o_pulse       (cfg_pulse),
        .o_pkt_ok      (),
        .o_pkt_err     ()
    );

    // 配置启用时用 PC 给的变调步进，否则退回板上拨码（保持原来的用法）
    wire signed [8:0] dphase_use = cfg_en ? cfg_dphase[8:0] : pitch_dphase;

    // ------------------------------------------------------------------
    wire signed [15:0] proc_l, proc_r;
    wire               proc_l_valid, proc_r_valid;

    voice_proc #(.AW(AW)) u_proc_l (
        .clk       (clk),
        .rst_n     (reset_n),
        .mode      (mode),
        .dphase    (dphase_use),
        .ring_freq (ring_freq),
        .echo_taps (echo_taps),
        .echo_gain (echo_gain),
        .din       (src_left),
        .din_valid (src_valid),
        .dout      (proc_l),
        .dout_valid(proc_l_valid)
    );

    voice_proc #(.AW(AW)) u_proc_r (
        .clk       (clk),
        .rst_n     (reset_n),
        .mode      (mode),
        .dphase    (dphase_use),
        .ring_freq (ring_freq),
        .echo_taps (echo_taps),
        .echo_gain (echo_gain),
        .din       (src_right),
        .din_valid (src_valid),
        .dout      (proc_r),
        .dout_valid(proc_r_valid)
    );

    // ------------------------------------------------------------------
    //  年龄音质着色（气声 / 微颤 / 高频倾斜），逐样本三级流水，延迟 3 拍。
    //  三个系数由上位机按年龄段算好，FPGA 只做定点乘加；
    //  cfg_en=0 时内部直通（en=0），逐位不影响原来的直通链路。
    // ------------------------------------------------------------------
    wire signed [15:0] tint_l, tint_r;
    wire               tint_l_v, tint_r_v;

    age_tint u_tint_l (
        .clk(clk), .rst_n(reset_n), .en(cfg_en),
        .coef_breath(cfg_breath), .coef_shim(cfg_shim), .coef_tilt(cfg_tilt),
        .din(proc_l), .din_valid(proc_l_valid),
        .dout(tint_l), .dout_valid(tint_l_v)
    );
    age_tint u_tint_r (
        .clk(clk), .rst_n(reset_n), .en(cfg_en),
        .coef_breath(cfg_breath), .coef_shim(cfg_shim), .coef_tilt(cfg_tilt),
        .din(proc_r), .din_valid(proc_r_valid),
        .dout(tint_r), .dout_valid(tint_r_v)
    );

    // 效果输出锁存：24.414 kHz 的帧率下有 2048 个系统周期可用，
    // 处理链远在此之前就完成了，所以直接锁存即可
    reg signed [15:0] tx_l_reg, tx_r_reg;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            tx_l_reg <= 16'sd0;
            tx_r_reg <= 16'sd0;
        end else begin
            if (tint_l_v) tx_l_reg <= tint_l;
            if (tint_r_v) tx_r_reg <= tint_r;
        end
    end

    assign tx_left  = tx_l_reg;
    assign tx_right = tx_r_reg;

    // ------------------------------------------------------------------
    //  左右声道独立电平表 / 削波 / 立体声统计
    //
    //  原来 led[7:3] 只是**左声道**的一根电平条，右声道过载、左右装反、
    //  两路其实一样——这些在 LED 上完全看不出来。改成每声道各两颗：
    //      led[3] 左有信号     led[4] 右有信号
    //      led[5] 左削波       led[6] 右削波
    //      led[7] 左右确实不同（相关性统计）
    //  led[1:0] 仍然是效果模式，led[2] 仍然是 I2C 配置完成。
    // ------------------------------------------------------------------
    wire [15:0] meter_lvl_l, meter_lvl_r;
    wire        meter_sig_l, meter_sig_r, meter_clip_l, meter_clip_r, meter_stereo;

    stereo_meter u_meter (
        .clk       (clk),
        .rst_n     (reset_n),
        .valid     (proc_l_valid),
        .din_l     (proc_l),
        .din_r     (proc_r),
        .lvl_l     (meter_lvl_l),
        .lvl_r     (meter_lvl_r),
        .sig_l     (meter_sig_l),
        .sig_r     (meter_sig_r),
        .clip_l    (meter_clip_l),
        .clip_r    (meter_clip_r),
        .stereo    (meter_stereo)
    );

    // ------------------------------------------------------------------
    //  LED：低两位显示效果模式，第三位显示配置完成，其余按声道分开
    // ------------------------------------------------------------------
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) led <= 8'd0;
        else begin
            led[1:0] <= mode;
            led[2]   <= cfg_done;
            led[3]   <= meter_sig_l;
            led[4]   <= meter_sig_r;
            led[5]   <= meter_clip_l;
            led[6]   <= meter_clip_r;
            led[7]   <= meter_stereo;
        end
    end

endmodule
