# 定向探针仿真。
# 注意：这里用有界时长 run 而不是 "run all" —— 该 TB 没有 $finish，
# "run all" 会让 xsim 一直跑下去直到被外部杀掉（表现为 0 字节输出）。
run 20000 ns
quit -force
