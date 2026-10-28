# separation_core 的独立综合（out_of_context）
# 只为了拿资源占用和最差时序，不做实现。
set part   "xc7a35tfgg484-2"
set outdir "vivado_prj/synth_sep"
file mkdir $outdir

read_verilog [list rtl/fft_twiddle.v rtl/fft1024.v rtl/win1024.v \
                   rtl/pitch_recip.v rtl/separation_core.v]

# 50 MHz 时钟约束。必须走 read_xdc：create_clock 要"有打开的设计"才能执行，
# 在 synth_design 之前直接敲会报 Common 17-53 "No open design"。
read_xdc constr/synth_sep.xdc

synth_design -top separation_core -part $part -mode out_of_context

report_utilization    -file $outdir/util.rpt
report_timing_summary -file $outdir/timing.rpt
puts "=== DONE ==="
