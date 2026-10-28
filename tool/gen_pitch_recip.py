# -*- coding: utf-8 -*-
"""
gen_pitch_recip.py —— 生成"自相关迟滞 → 基频(bin 单位 Q8)"的倒数表

自相关在第 lag 个迟滞处出峰，对应基频 f0 = FS/lag。
频率改写成"bin 单位"（1 bin = FS/NFFT）后：

    f0_bin = NFFT / lag            →  f0_bin_q8 = (NFFT << 8) / lag

硬件里不想放除法器，就把 481 项倒数表做成 ROM（lag = 32..512）。
查出 f0_bin_q8 之后，第 h 次谐波的位置 = (h * f0_bin_q8) >> 8，
一个乘法加移位就够了。
"""
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "rtl", "pitch_recip.v"))
LAG_LO, LAG_HI = 32, 512
NFFT = 1024


def main():
    L = []
    L.append("// ============================================================================")
    L.append("//  pitch_recip.v  自相关迟滞 → 基频(bin 单位 Q8) 的倒数表")
    L.append("//")
    L.append("//  **由 tool/gen_pitch_recip.py 生成，不要手改。**")
    L.append("//  f0_bin_q8 = (1024 << 8) / lag，lag = 32..512")
    L.append("// ============================================================================")
    L.append("`timescale 1ns / 1ps")
    L.append("")
    L.append("module pitch_recip (")
    L.append("    input  wire [9:0]   lag,")
    L.append("    output reg  [23:0]  f0_q8")
    L.append(");")
    L.append("")
    L.append("    always @(*) begin")
    L.append("        case (lag)")
    for lag in range(LAG_LO, LAG_HI + 1):
        v = int(round((NFFT << 8) / lag))
        v = max(0, min((1 << 24) - 1, v))
        L.append("            10'd%-4d : f0_q8 = 24'd%-6d;" % (lag, v))
    L.append("            default   : f0_q8 = 24'd0;")
    L.append("        endcase")
    L.append("    end")
    L.append("")
    L.append("endmodule")
    L.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("写出 %s（lag %d..%d）" % (OUT, LAG_LO, LAG_HI))
    # 自检：lag=100 时 f0_bin=10.24
    chk = int(round((NFFT << 8) / 100))
    print("lag=100 -> f0_bin_q8=%d (=%.3f bin = %.1f Hz @24.414k)"
          % (chk, chk / 256.0, chk / 256.0 * 24414 / 1024))


if __name__ == "__main__":
    main()
