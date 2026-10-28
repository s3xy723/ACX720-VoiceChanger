# -*- coding: utf-8 -*-
"""
生成 ACX720 变声工程所需的两个查找表：
  1. hann_rom  : 512 点 Hann 窗, 用于双抽头交叉淡化
  2. sine_rom  : 256 点正弦, 用于环形调制 NCO
输出: ../rtl/wave_rom.v
"""
import math
import os

HANN_N = 512
SINE_N = 256

out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rtl")
out_path = os.path.normpath(os.path.join(out_dir, "wave_rom.v"))

lines = []
lines.append("// ------------------------------------------------------------")
lines.append("//  wave_rom.v   自动生成,请勿手工编辑")
lines.append("//  由 tool/gen_wave_rom.py 生成")
lines.append("//    hann_rom : 512 点 Hann 窗 w(d) = 0.5 - 0.5*cos(2*pi*d/N)")
lines.append("//    sine_rom : 256 点正弦, 有符号 16 位")
lines.append("// ------------------------------------------------------------")
lines.append("")
lines.append("`timescale 1ns / 1ps")
lines.append("")
lines.append("module hann_rom (")
lines.append("    input  wire [8:0]  addr,")
lines.append("    output reg  [15:0] dout")
lines.append(");")
lines.append("")
lines.append("    reg [15:0] mem [0:%d];" % (HANN_N - 1))
lines.append("")
lines.append("    initial begin")

for i in range(HANN_N):
    # Hann: w = 0.5 - 0.5*cos(2*pi*d/N), d = i。
    # 用 HANN_N 而非 HANN_N-1 作周期，保证 w(i) + w(i+256) == 1 严格成立
    w = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / HANN_N)
    v = int(round(65535.0 * w))
    if v < 0:
        v = 0
    if v > 65535:
        v = 65535
    lines.append("        mem[%3d] = 16'h%04X;" % (i, v))

lines.append("    end")
lines.append("")
lines.append("    always @(*) dout = mem[addr];")
lines.append("")
lines.append("endmodule")
lines.append("")
lines.append("// ------------------------------------------------------------")
lines.append("")
lines.append("module sine_rom (")
lines.append("    input  wire [7:0]  addr,")
lines.append("    output reg  [15:0] dout")
lines.append(");")
lines.append("")
lines.append("    reg [15:0] mem [0:%d];" % (SINE_N - 1))
lines.append("")
lines.append("    initial begin")

for i in range(SINE_N):
    s = math.sin(2.0 * math.pi * i / SINE_N)
    v = int(round(32767.0 * s))
    # 必须先按有符号范围钳位，再转两补数；顺序颠倒会把负半周压成 +32767
    if v > 32767:
        v = 32767
    if v < -32768:
        v = -32768
    if v < 0:
        v = v & 0xFFFF
    lines.append("        mem[%3d] = 16'h%04X;" % (i, v))

lines.append("    end")
lines.append("")
lines.append("    always @(*) dout = mem[addr];")
lines.append("")
lines.append("endmodule")
lines.append("")

with open(out_path, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(lines))

print("WROTE " + out_path)
print("hann entries = %d, sine entries = %d" % (HANN_N, SINE_N))

# ---- 校验 1: Hann 互补性 w(i) + w(i+N/2) 应恒等于 65535 ----
def hann_v(i):
    w = 0.5 - 0.5 * math.cos(2.0 * math.pi * (i % HANN_N) / HANN_N)
    return int(round(65535.0 * w))

err = 0
for i in range(HANN_N // 2):
    s = hann_v(i) + hann_v(i + HANN_N // 2)
    if abs(s - 65535) > 2:
        err += 1
print("hann complement check: %d/%d entries deviate (expect 0)"
      % (err, HANN_N // 2))

# ---- 校验 2: sine 负半周必须真的为负 ----
def sine_v(i):
    v = int(round(32767.0 * math.sin(2.0 * math.pi * i / SINE_N)))
    v = max(-32768, min(32767, v))
    return v

neg_ok = all(sine_v(i) < 0 for i in range(SINE_N // 2 + 1, SINE_N))
print("sine negative half OK = %s   (min=%d, max=%d)"
      % (neg_ok, min(sine_v(i) for i in range(SINE_N)),
         max(sine_v(i) for i in range(SINE_N))))
