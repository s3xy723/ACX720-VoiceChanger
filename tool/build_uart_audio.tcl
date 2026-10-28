# 路径全部相对脚本自身推导：把这个仓库克隆到任何位置都能直接跑。
# （原来这里写死了 C:/Users/Administrator/... 的绝对路径，换机器必失效。）
set root [file normalize [file join [pwd] ..]]
open_project [file join $root vivado_prj voice_changer voice_changer.xpr]

foreach f [list $root/rtl/uart_rx.v $root/rtl/uart_audio_rx.v $root/rtl/uart_tx.v \
               $root/rtl/cfg_parser.v $root/rtl/age_tint.v] {
    if {[llength [get_files -quiet $f]] == 0} {
        add_files -fileset sources_1 $f
        puts "ADDED $f"
    }
}
set_property top voice_changer_top [current_fileset]
update_compile_order -fileset sources_1

reset_run synth_1
launch_runs synth_1 -jobs 4
wait_on_run synth_1
puts "SYNTH_STATUS: [get_property STATUS [get_runs synth_1]]"
puts "SYNTH_PROGRESS: [get_property PROGRESS [get_runs synth_1]]"
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synthesis failed"
}

launch_runs impl_1 -to_step write_bitstream -jobs 4
wait_on_run impl_1
puts "IMPL_STATUS: [get_property STATUS [get_runs impl_1]]"
puts "IMPL_PROGRESS: [get_property PROGRESS [get_runs impl_1]]"
close_project
exit
