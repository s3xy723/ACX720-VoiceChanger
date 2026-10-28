set part   "xc7a35tfgg484-2"
set outdir "vivado_prj/synth_fft"
file mkdir $outdir
read_verilog [list rtl/fft_twiddle.v rtl/fft1024.v]
synth_design -top fft1024 -part $part -mode out_of_context
report_utilization    -file $outdir/util.rpt
report_timing_summary -file $outdir/timing.rpt
puts "=== DONE ==="
