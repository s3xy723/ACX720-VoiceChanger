puts "=== open hardware ==="
open_hw
connect_hw_server
puts "targets: [get_hw_targets]"
open_hw_target
set devs [get_hw_devices]
puts "devices count=[llength $devs]"
foreach d $devs {
    puts "DEV: $d"
    foreach prop {PART IDCODE REGISTER.USERCODE} {
        if {![catch {set v [get_property $prop $d]}]} {
            puts "  $prop = $v"
        }
    }
}
close_hw_target
exit
