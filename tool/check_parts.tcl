puts "=== Vivado version ==="
puts [version -short]
puts "=== parts matching *7a35* ==="
set p [get_parts -quiet *7a35*]
if {[llength $p] == 0} {
    puts "NO_PART_MATCH"
} else {
    foreach x $p { puts $x }
}
puts "=== parts matching xc7a* (first 40) ==="
set q [get_parts -quiet xc7a*]
puts "count=[llength $q]"
foreach x [lrange $q 0 39] { puts $x }
exit
