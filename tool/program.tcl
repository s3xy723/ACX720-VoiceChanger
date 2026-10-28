# ============================================================================
#  program.tcl   把比特流下载到 ACX720（Digilent JTAG-HS2）
#
#  用法：
#      vivado -mode batch -source tool/program.tcl
#  或指定 bit 文件：
#      vivado -mode batch -source tool/program.tcl -tclargs path/to/xxx.bit
#
#  注意（Vivado 2018.3 的几个坑，本脚本已经规避）：
#      1. 命令是 open_hw，不是 open_hw_manager
#      2. connect_hw_server 不支持 -allow_non_jtag
#      3. refresh_hw_device 不支持 -update_hw_programs
#      4. Digilent HS2 的 target 没有 PARAM.FREQ 属性，设了会报 44-126
# ============================================================================

set TOP_DIR [file normalize [file dirname [info script]]/..]
if { $argc > 0 } {
    set BITFILE [lindex $argv 0]
} else {
    set BITFILE "$TOP_DIR/vivado_prj/voice_changer.bit"
}

if { ![file exists $BITFILE] } {
    puts "ERROR: bitfile not found: $BITFILE"
    exit 1
}
puts "Programming: $BITFILE"

open_hw
connect_hw_server
open_hw_target

set devs [get_hw_devices]
puts "HW devices:"
foreach d $devs { puts "  $d  [get_property PART $d]" }

# ACX720 的 35T，IDCODE 0x0362D093
set dev ""
foreach d $devs {
    set p [get_property PART $d]
    if { [regexp {xc7a35t} $p] } { set dev $d }
}
if { $dev eq "" } {
    # 兜底：取第一个
    set dev [lindex $devs 0]
}
puts "Target device: $dev"

current_hw_device $dev
refresh_hw_device $dev

set_property PROGRAM.FILE $BITFILE $dev
program_hw_devices $dev
refresh_hw_device $dev

puts "================ PROGRAM DONE ================"
# 判断成功与否看上面的 "End of startup status: HIGH"。
# 注意：Vivado 2018.3 的 hw_device 既没有 PROGRAM.STATUS 也没有 STATUS 属性，
# 去读会报 [Labtoolstcl 44-56]，所以这里不要读任何属性。
close_hw_target
quit
