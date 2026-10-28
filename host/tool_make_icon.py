# -*- coding: utf-8 -*-
"""
tool_make_icon.py  ——  给 EXE 生成一个 32×32 的图标

不引 PIL：直接按 ICO 格式手写字节。内容是两条错开的波形（输入 / 输出），
正好对应程序里那张频谱对照图的意思。
"""

import os
import struct

W = H = 32
BG = (0xF5, 0xF7, 0xFA, 255)      # BGRA
C_IN = (0xD0, 0x6F, 0x1F, 255)    # 蓝（BGR）
C_OUT = (0x10, 0x72, 0xD9, 255)   # 橙（BGR）


def wave_y(x, amp, phase, base):
    import math
    return base + amp * math.sin(2.0 * math.pi * (x / 9.0) + phase)


def build_pixels():
    px = [[BG for _ in range(W)] for _ in range(H)]
    for x in range(2, W - 2):
        y1 = int(round(wave_y(x, 5.0, 0.0, 10.0)))
        y2 = int(round(wave_y(x, 6.5, 1.1, 21.0)))
        for dy in (-1, 0, 1):
            y = y1 + dy
            if 0 <= y < H:
                px[y][x] = C_IN
            y = y2 + dy
            if 0 <= y < H:
                px[y][x] = C_OUT
    return px


def write_ico(path):
    px = build_pixels()
    # BITMAPINFOHEADER：高度写 2*H（XOR 图 + AND 掩码）
    bih = struct.pack("<IiiHHIIiiII", 40, W, H * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    body = bytearray()
    for y in range(H - 1, -1, -1):          # DIB 自下而上
        for x in range(W):
            b, g, r, a = px[y][x]
            body += bytes((b, g, r, a))
    and_mask = bytearray(((W + 31) // 32) * 4 * H)   # 全 0 = 完全不透明
    img = bih + bytes(body) + bytes(and_mask)

    head = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(img), 6 + 16)
    with open(path, "wb") as f:
        f.write(head + entry + img)
    return path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    write_ico(out)
    print("wrote", out, os.path.getsize(out), "bytes")
