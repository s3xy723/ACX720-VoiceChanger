# ============================================================================
#  run_synth.tcl   在 Vivado batch 模式下建工程并综合，不做实现（Impl）
#
#  为什么只做综合：constr/voice_changer.xdc 里的音频口 5 根线被注释掉了
#  （标准版 ACX720 不带 codec），实现阶段会因为端口未约束而报错。
#  综合阶段不吃这一套，正好用来验证 RTL 语法、推断 BRAM/DSP、看资源量。
#
#  用法（Windows Git Bash）：
#      vivado -mode batch -source tool/run_synth.tcl
#  或：
#      vivado -mode batch -source tool/run_synth.tcl -tclargs synth
#      vivado -mode batch -source tool/run_synth.tcl -tclargs sim
# ============================================================================

set MODE "synth"
if { $argc > 0 } { set MODE [lindex $argv 0] }

set TOP_DIR [file normalize [file dirname [info script]]/..]
set PRJ_DIR "$TOP_DIR/vivado_prj"
set RTL_DIR "$TOP_DIR/rtl"
set XDC_DIR "$TOP_DIR/constr"
set XSIM_DIR "$TOP_DIR/sim_src"

set PART "xc7a35tfgg484-2"
set SRC_FILES [list \
    "$RTL_DIR/wave_rom.v" \
    "$RTL_DIR/pitch_shift.v" \
    "$RTL_DIR/effect_aux.v" \
    "$RTL_DIR/voice_proc.v" \
    "$RTL_DIR/stereo_meter.v" \
    "$RTL_DIR/i2c_bit_shift.v" \
    "$RTL_DIR/i2c_control.v" \
    "$RTL_DIR/i2c_init_dev.v" \
    "$RTL_DIR/es8388_init_table.v" \
    "$RTL_DIR/es8388_init.v" \
    "$RTL_DIR/i2s_slave.v" \
    "$RTL_DIR/voice_changer_top.v" \
]

file mkdir $PRJ_DIR

if { $MODE eq "sim" } {
    file delete -force "$PRJ_DIR/sim_prj"
    create_project sim_prj "$PRJ_DIR/sim_prj" -part $PART -force
    foreach f $SRC_FILES { read_verilog $f }
    foreach f [glob -nocomplain "$XSIM_DIR/*.v"] { read_verilog -library work $f }
    set_property top tb_top [current_fileset -simset]
    set_property top tb_top [current_fileset]
    update_compile_order -fileset sim_1
    launch_xsim tb_top -simset [get_filesets sim_1] -mode behavioral
    run all
    quit
}

create_project voice_changer "$PRJ_DIR/voice_changer" -part $PART -force
set_property target_language Verilog [current_project]

foreach f $SRC_FILES {
    if { [file exists $f] } {
        read_verilog $f
    } else {
        puts "ERROR: source not found: $f"
        exit 1
    }
}

read_xdc "$XDC_DIR/voice_changer.xdc"

set_property top voice_changer_top [current_fileset]
update_compile_order -fileset sources_1

# ---------------------------------------------------------------------------
#  综合。策略针对小器件做了收敛优化，ram_style 让 4096x16 的延迟线
#  强制进 Block RAM 而不是吃 2000 个 LUT。
# ---------------------------------------------------------------------------
synth_design -top voice_changer_top -part $PART \
    -directive AreaOptimized_high \
    -resource_sharing on

# 内存吃紧的机器上写 DCP 会 OOM，而本脚本只需要报告，因此默认跳过。
# 需要 DCP 时把本文件里下面这行的行首 # 去掉。
# write_checkpoint -force "$PRJ_DIR/post_synth.dcp"

# 注意：这里是直接跑 synth_design（不走 launch_runs），所以没有 synth_1 这个
# run 对象，用 open_run synth_1 会报 "has not been launched"。
# synth_design 结束后设计已经在内存里，直接出报告即可。
report_utilization -file "$PRJ_DIR/utilization.rpt"
report_utilization -hierarchical -file "$PRJ_DIR/util_hier.rpt"
report_timing_summary -max_paths 10 -file "$PRJ_DIR/timing.rpt"
report_clocks -file "$PRJ_DIR/clocks.rpt"

# 把关键信息回显到 stdout，方便直接看
puts "================ UTILIZATION ================"
set fp [open "$PRJ_DIR/utilization.rpt" r]
set content [read $fp]
close $fp
foreach line [split $content "\n"] {
    if { [regexp {Slice LUTs|Block RAM Tile|DSPs|Slice Registers|Flip Flop} $line] } {
        puts $line
    }
}

puts "================ TIMING (WNS) ================"
set fp [open "$PRJ_DIR/timing.rpt" r]
set content [read $fp]
close $fp
foreach line [split $content "\n"] {
    if { [regexp {WNS\(ns\)|TNS\(ns\)|WHS\(ns\)} $line] } { puts $line }
}

puts "================ DONE ================"
puts "reports in: $PRJ_DIR"
close_project
quit
