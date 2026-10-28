puts "=== JTAG / Hardware Manager check ==="
open_hw_manager
connect_hw_server
puts "targets: [get_hw_targets]"
open_hw_target
set devs [get_hw_devices]
puts "devices count=[llength $devs]"
foreach d $devs {
    puts "DEV: $d"
    foreach prop {PART IDCODE REGISTER.USERCODE PROGRAM.STATUS REGISTER.CONFIG_STATUS REGISTER.IS_VERIFIED} {
        if {![catch {set v [get_property $prop $d]}]} {
            puts "  $prop = $v"
        } else {
            puts "  $prop = <not available>"
        }
    }
}
close_hw_target
exit
