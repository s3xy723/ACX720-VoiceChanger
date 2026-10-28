# ============================================================================
#  voice_changer.xdc    ACX720-V3 (XC7A35T) 引脚约束
#
#  引脚来源（两份相互印证，完全一致）：
#    1) 官方《ACX720-V3管脚分配表2024_11_13.xlsx》
#    2) 官方例程 ch35_CoreCourse_GHRD_I2S_RX 的 constrs_1/new/IO.xdc
#
#  板载音频 codec 是 ES8388（注意：不是 WM8731）：
#      XCLK(MCLK) V20  FPGA -> codec   本工程 12.5 MHz
#      BCLK       U20  codec -> FPGA
#      LRCK       W20  codec -> FPGA
#      ADCDAT     AA21 codec -> FPGA
#      DACDAT     AA20 FPGA -> codec
#      I2C        SCL=M13  SDA=M16  （板载 I2C 总线，ES8388 地址 0x10）
# ============================================================================

set_property BITSTREAM.CONFIG.UNUSEDPIN Pullnone [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE 33 [current_design]
set_property CONFIG_MODE SPIx4 [current_design]

# ---------------------------- 系统时钟 / 复位 ----------------------------
set_property PACKAGE_PIN Y18 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
create_clock -period 20.000 -name clk [get_ports clk]

# 复位按键 S4（管脚表注明 S4 为复位按键）
set_property PACKAGE_PIN B21 [get_ports reset_n]
set_property IOSTANDARD LVCMOS33 [get_ports reset_n]
set_property PULLUP true [get_ports reset_n]

# ---------------------------- 拨码开关 sw[7:0] ----------------------------
set_property PACKAGE_PIN G22 [get_ports {sw[0]}]
set_property PACKAGE_PIN D22 [get_ports {sw[1]}]
set_property PACKAGE_PIN E22 [get_ports {sw[2]}]
set_property PACKAGE_PIN G21 [get_ports {sw[3]}]
set_property PACKAGE_PIN E21 [get_ports {sw[4]}]
set_property PACKAGE_PIN D21 [get_ports {sw[5]}]
set_property PACKAGE_PIN C22 [get_ports {sw[6]}]
set_property PACKAGE_PIN B22 [get_ports {sw[7]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[*]}]

# ---------------------------- LED led[7:0] ----------------------------
set_property PACKAGE_PIN M22 [get_ports {led[0]}]
set_property PACKAGE_PIN N22 [get_ports {led[1]}]
set_property PACKAGE_PIN L21 [get_ports {led[2]}]
set_property PACKAGE_PIN K21 [get_ports {led[3]}]
set_property PACKAGE_PIN K22 [get_ports {led[4]}]
set_property PACKAGE_PIN J22 [get_ports {led[5]}]
set_property PACKAGE_PIN H22 [get_ports {led[6]}]
set_property PACKAGE_PIN M21 [get_ports {led[7]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[*]}]

# ---------------------------- I2C（ES8388 配置口）----------------------------
set_property PACKAGE_PIN M13 [get_ports i2c_sclk]
set_property PACKAGE_PIN M16 [get_ports i2c_sdat]
set_property IOSTANDARD LVCMOS33 [get_ports i2c_sclk]
set_property IOSTANDARD LVCMOS33 [get_ports i2c_sdat]
set_property SLEW SLOW [get_ports i2c_sclk]
set_property SLEW SLOW [get_ports i2c_sdat]
set_property PULLUP true [get_ports i2c_sclk]
set_property PULLUP true [get_ports i2c_sdat]

# ---------------------------- ES8388 数字音频口 ----------------------------
set_property PACKAGE_PIN V20  [get_ports aud_mclk]
set_property PACKAGE_PIN U20  [get_ports aud_bclk]
set_property PACKAGE_PIN W20  [get_ports aud_lrclk]
set_property PACKAGE_PIN AA20 [get_ports aud_dacdat]
set_property PACKAGE_PIN AA21 [get_ports aud_adcdat]
set_property IOSTANDARD LVCMOS33 [get_ports aud_mclk]
set_property IOSTANDARD LVCMOS33 [get_ports aud_bclk]
set_property IOSTANDARD LVCMOS33 [get_ports aud_lrclk]
set_property IOSTANDARD LVCMOS33 [get_ports aud_dacdat]
set_property IOSTANDARD LVCMOS33 [get_ports aud_adcdat]
set_property SLEW SLOW [get_ports aud_mclk]
set_property SLEW SLOW [get_ports aud_bclk]
set_property SLEW SLOW [get_ports aud_lrclk]
set_property SLEW SLOW [get_ports aud_dacdat]

# BCLK / LRCK 是 codec 送给 FPGA 的信号。本设计只用它们做同步后的沿检测
# （50 MHz 过采样），不当时钟网络用，因此不需要时钟约束；
# 但 Vivado 若把它们连到了寄存器的时钟端会报 CLOCK_DEDICATED_ROUTE，
# 这里按官方例程的写法一并放开，避免实现阶段报错。
set_property CLOCK_DEDICATED_ROUTE FALSE [get_nets {aud_bclk_IBUF}]
set_property CLOCK_DEDICATED_ROUTE FALSE [get_nets {aud_lrclk_IBUF}]

## ------------------------------------------------------------------------
##  给单片机的 SPI 镜像总线（ACX720-V3 的 GPIO0 32 位通用 IO 扩展口）
##  本工程未使用该扩展口，三个引脚均空闲。若你的板子批次不同，
##  换成 GPIO0 排针上任意三个不相邻冲突的空位即可（Verilog 不用动）。
##  接线：mcu_sclk -> STM32 PA5 (SPI1_SCK), mcu_csn -> PA4 (SPI1_NSS/EXTI4),
##        mcu_mosi -> PA7 (SPI1_MOSI)，GND 必须共地
## ------------------------------------------------------------------------
set_property PACKAGE_PIN A13 [get_ports mcu_sclk]
set_property PACKAGE_PIN A14 [get_ports mcu_csn]
set_property PACKAGE_PIN A15 [get_ports mcu_mosi]
set_property IOSTANDARD LVCMOS33 [get_ports {mcu_sclk mcu_csn mcu_mosi}]
set_property SLEW SLOW [get_ports {mcu_sclk mcu_csn mcu_mosi}]

# ------------------------------------------------------------------------
#  ACX720 板载 CH340 UART：PC -> FPGA 音频输入（1.5 Mbaud 8N1）
#  FPGA_UART_RX = J21（来自官方 ch06_uart_rx 例程 XDC）
# ------------------------------------------------------------------------
set_property PACKAGE_PIN J21 [get_ports uart_rx]
set_property IOSTANDARD LVCMOS33 [get_ports uart_rx]
set_property PULLUP true [get_ports uart_rx]

# FPGA_UART_TX = M15：每收到一帧回 0xA5，用于上位机链路自检
set_property PACKAGE_PIN M15 [get_ports uart_tx]
set_property IOSTANDARD LVCMOS33 [get_ports uart_tx]
