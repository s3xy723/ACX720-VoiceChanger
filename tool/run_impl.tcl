# ============================================================================
#  run_impl.tcl   综合 -> 实现 -> 生成比特流（一步到位）
#
#  用法：
#      vivado -mode batch -source tool/run_impl.tcl
#
#  产出：
#      vivado_prj/voice_changer.bit    可直接下载的比特流
#      vivado_prj/utilization.rpt      资源
#      vivado_prj/timing.rpt           时序（WNS 必须 >= 0）
#      vivado_prj/io_report.rpt        引脚分配核对
# ============================================================================

set TOP_DIR [file normalize [file dirname [info script]]/..]
set PRJ_DIR "$TOP_DIR/vivado_prj"
set RTL_DIR "$TOP_DIR/rtl"
set XDC_DIR "$TOP_DIR/constr"

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
    "$RTL_DIR/i2s_mirror_tx.v" \
    "$RTL_DIR/voice_changer_top.v" \
]

file mkdir $PRJ_DIR
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

puts "================ SYNTH ================"
synth_design -top voice_changer_top -part $PART \
    -directive AreaOptimized_high \
    -resource_sharing on
report_utilization -file "$PRJ_DIR/utilization.rpt"
report_utilization -hierarchical -file "$PRJ_DIR/util_hier.rpt"

puts "================ OPT / PLACE / ROUTE ================"
opt_design
place_design
route_design

report_utilization -file "$PRJ_DIR/util_impl.rpt"
report_timing_summary -max_paths 20 -file "$PRJ_DIR/timing.rpt"
report_io -file "$PRJ_DIR/io_report.rpt"
report_clock_utilization -file "$PRJ_DIR/clk_util.rpt"

puts "================ BITSTREAM ================"
write_bitstream -force "$PRJ_DIR/voice_changer.bit"
write_debug_probes -force "$PRJ_DIR/voice_changer.ltx"

puts "================ SUMMARY ================"
foreach f [list "$PRJ_DIR/util_impl.rpt" "$PRJ_DIR/timing.rpt"] {
    set fp [open $f r]
    set content [read $fp]
    close $fp
    foreach line [split $content "\n"] {
        if { [regexp {Slice LUTs|Block RAM Tile|DSPs|Slice Registers|WNS\(ns\)|TNS\(ns\)|WHS\(ns\)|THS\(ns\)} $line] } {
            puts $line
        }
    }
}
puts "BIT: $PRJ_DIR/voice_changer.bit"
puts "================ DONE ================"
close_project
quit
