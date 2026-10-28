# -*- coding: utf-8 -*-
"""
gen_fft_rom.py —— 生成 1024 点 FFT 的旋转因子 ROM（rtl/fft_twiddle.v）

DIF（Gentleman-Sande）形式下第 s 级用的旋转因子是 exp(-j*2*pi*(j<<s)/1024)，
所以只需要 N/2 = 512 个值，索引 = j<<s 直接就是位拼接，硬件里连乘法都不用。

存成 Q15 复数：16 位实部 + 16 位虚部。
"""
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "rtl", "fft_twiddle.v"))
N = 1024
HALF = N // 2


def q15(x):
    v = int(round(x * 32767.0))
    return max(-32768, min(32767, v))


def s16(v):
    """Verilog 的有符号字面量：负号要在位宽前面（16'sd-5 是非法的）。"""
    return ("-16'sd%d" % (-v)) if v < 0 else ("16'sd%d" % v)


def main():
    re = [q15(np.cos(-2.0 * np.pi * k / N)) for k in range(HALF)]
    im = [q15(np.sin(-2.0 * np.pi * k / N)) for k in range(HALF)]

    L = []
    L.append("// ============================================================================")
    L.append("//  fft_twiddle.v  1024 点 FFT 的旋转因子（Q15 复数，512 项）")
    L.append("//")
    L.append("//  **本文件由 tool/gen_fft_rom.py 生成，不要手改。**")
    L.append("//")
    L.append("//  用 DIF（Gentleman-Sande）分解：第 s 级、组内第 j 个蝶形用的旋转因子是")
    L.append("//      exp(-j*2*pi*(j<<s)/1024)")
    L.append("//  所以地址就是 j 左移 s 位 —— 硬件里只需要一个移位，不需要乘法。")
    L.append("//  512 项 × 32 位 = 16 Kbit，正好一块 BRAM18。")
    L.append("// ============================================================================")
    L.append("`timescale 1ns / 1ps")
    L.append("")
    L.append("module fft_twiddle (")
    L.append("    input  wire [8:0]  addr,")
    # 必须显式指定成 block ROM：只用 case 语句的话 Vivado 会推成分布式 LUT ROM，
    # 512x16x2 = 16 Kbit 要吃掉 8000 多个 LUT（占 35T 的 40%）。
    # 这个坑是 tool/synth_fft.tcl 综合后报 "fft_twiddle | tw_re | 512x16 | LUT"
    # 才发现的。
    L.append("    (* rom_style = \"block\" *) output reg  signed [15:0] tw_re,")
    L.append("    (* rom_style = \"block\" *) output reg  signed [15:0] tw_im")
    L.append(");")
    L.append("")
    L.append("    always @(*) begin")
    L.append("        case (addr)")
    for k in range(HALF):
        L.append("            9'd%-4d : begin tw_re = %s; tw_im = %s; end"
                 % (k, s16(re[k]).rjust(12), s16(im[k]).rjust(12)))
    L.append("            default  : begin tw_re = 16'sd0; tw_im = 16'sd0; end")
    L.append("        endcase")
    L.append("    end")
    L.append("")
    L.append("endmodule")
    L.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("写出 %s（%d 项）" % (OUT, HALF))


if __name__ == "__main__":
    main()
