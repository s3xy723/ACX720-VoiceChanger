# separation_core 独立综合用的时钟约束
# 板上系统时钟 50 MHz（ES8388 跑主模式，MCLK 12.5 MHz -> LRCK = fs = 24.414 kHz）。
# 没有这个约束 report_timing_summary 是空的，等于没查时序。
create_clock -period 20.000 -name clk [get_ports clk]
