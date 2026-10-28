# -*- coding: utf-8 -*-
"""
gen_win1024_rom.py —— 生成 1024 点周期 Hann 窗（rtl/win1024.v）

周期 Hann：w[i] = 0.5 - 0.5*cos(2*pi*i/1024)，分母必须是 N（不是 N-1），
这样 w[i] + w[i+512] = 1 严格成立，重叠相加的互补关系才不会被破坏
（这条坑在 2026-10-10 的 wave_rom 上踩过一次）。

存成 Q15 无符号（0..32767）。
"""
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "rtl", "win1024.v"))
N = 1024


def main():
    L = []
    L.append("// ============================================================================")
    L.append("//  win1024.v  1024 点周期 Hann 窗（Q15 无符号）")
    L.append("//")
    L.append("//  **由 tool/gen_win1024_rom.py 生成，不要手改。**")
    L.append("//  分母取 N=1024 而不是 N-1，保证 w[i]+w[i+512]=1（重叠相加的互补关系）。")
    L.append("// ============================================================================")
    L.append("`timescale 1ns / 1ps")
    L.append("")
    L.append("module win1024 (")
    L.append("    input  wire [9:0]  addr,")
    L.append("    output reg  [15:0] w")
    L.append(");")
    L.append("")
    L.append("    always @(*) begin")
    L.append("        case (addr)")
    for i in range(N):
        v = int(round(32767.0 * (0.5 - 0.5 * np.cos(2.0 * np.pi * i / N))))
        v = max(0, min(32767, v))
        L.append("            10'd%-4d : w = 16'd%-5d;" % (i, v))
    L.append("            default   : w = 16'd0;")
    L.append("        endcase")
    L.append("    end")
    L.append("")
    L.append("endmodule")
    L.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    # 自检互补关系
    w = np.array([int(round(32767.0 * (0.5 - 0.5 * np.cos(2 * np.pi * i / N))))
                  for i in range(N)])
    comp = int(np.abs(w[:N // 2] + w[N // 2:] - 32767).max())
    print("写出 %s" % OUT)
    print("互补性 max|w[i]+w[i+512]-32767| = %d（应为 0）" % comp)
    assert comp == 0


if __name__ == "__main__":
    main()
