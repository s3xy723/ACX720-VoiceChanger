#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 0.96" OLED（SSD1306 128x64）用的点阵字体表：mcu/.../oled_font.h

     python tool/gen_oled_font.py

为什么不手抄字体表：8x16 的 ASCII 96 个字 + 二十来个 16x16 汉字，手抄一遍
必然错几个（表现出来是某个字母缺一横，很难往回查）。这里直接从系统字体
渲染、二值化、再导出成 C 数组，改字号/换字体只要重跑一次。
字模格式与 SSD1306 的显存一致：**列优先**，每个字节是某一列的 8 个竖直像素，
低位在上。8x16 的字符占 8 列 × 2 页（16 字节）；16x16 的汉字占 16 列 × 2 页。
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "mcu", "f103_spi_mcp4922", "oled_font.h")

ASCII_FONT = "C:/Windows/Fonts/consola.ttf"
CJK_FONT = "C:/Windows/Fonts/msyh.ttc"

# 显示器上真正会用到的汉字（多了没用，每个占 32 字节 Flash）
CJK = "变调分离直通环形回声中置本地模型人伴奏半音增益深度链路断开关机"

W_ASCII, H_ASCII = 8, 16
W_CJK, H_CJK = 16, 16


def _render(ch, font, w, h, thr=110):
    """把单个字符渲染到 w×h 格子里，按墨迹 bbox 居中，灰度阈值二值化。

    直接用 "1" 模式 + LANCZOS 缩放会画出很脏的笔画（第一版汉字全是糊的），
    所以全程用灰度 "L" 渲染，最后一步才按阈值切成 0/1。
    """
    big = Image.new("L", (w * 3, h * 3), 0)
    ImageDraw.Draw(big).text((w, h), ch, font=font, fill=255)
    box = big.getbbox()
    cell = Image.new("L", (w, h), 0)
    if box:
        glyph = big.crop(box)
        gw, gh = glyph.size
        if gw > w or gh > h:
            k = min(w / float(gw), h / float(gh))
            glyph = glyph.resize((max(1, int(gw * k)), max(1, int(gh * k))), Image.LANCZOS)
            gw, gh = glyph.size
        cell.paste(glyph, ((w - gw) // 2, (h - gh) // 2))
    px = cell.load()
    # ★ 顺序必须是"先整页的 8 列，再下一页的 8 列"，不能按列交错：
    #   C 侧读的是 FONT8X16[c][i]（第 i 列上半页）和 [c][8+i]（下半页）。
    #   第一版写成 for x -> for page 交错输出，屏幕上每个字母都是错位的点。
    cols = []
    for page in range(h // 8):
        for x in range(w):
            b = 0
            for bit in range(8):
                if px[x, page * 8 + bit] >= thr:
                    b |= 1 << bit
            cols.append(b)
    return cols


def _ascii_table():
    try:
        font = ImageFont.truetype(ASCII_FONT, 14)
    except Exception:
        font = ImageFont.load_default()
    out = []
    for code in range(32, 128):
        cols = _render(chr(code), font, W_ASCII, H_ASCII)
        out.append(cols)
    return out


def _cjk_table():
    font = ImageFont.truetype(CJK_FONT, 15)
    out = []
    for ch in CJK:
        # 微软雅黑 16px 的汉字基本占满格子，往上挪 1 px 更居中
        cols = _render(ch, font, W_CJK, H_CJK)
        out.append((ch, cols))
    return out


def _emit(name, rows, per_line=8):
    lines = []
    for i, cols in enumerate(rows):
        body = ", ".join("0x%02X" % b for b in cols)
        lines.append("    { %s },%s" % (body, "" if i == len(rows) - 1 else ""))
    return "static const unsigned char %s[%d][%d] = {\n%s\n};\n" % (
        name, len(rows), len(rows[0]), "\n".join(lines))


def main():
    ascii_rows = _ascii_table()
    cjk_rows = _cjk_table()
    parts = []
    parts.append("/* 自动生成，别手改 —— 重新生成： python tool/gen_oled_font.py */\n")
    parts.append("#ifndef OLED_FONT_H\n#define OLED_FONT_H\n\n")
    parts.append("/* ASCII 32..127，8x16，列优先（每字节 = 一列的 8 个竖直像素，低位在上）*/\n")
    parts.append("#define FONT_ASCII_FIRST 32\n")
    parts.append(_emit("FONT8X16", ascii_rows))
    parts.append("\n/* 16x16 汉字表：UTF-8 解码后按码点查这张表 */\n")
    parts.append("typedef struct { unsigned int code; const unsigned char mat[32]; } CJK16_T;\n")
    entries = []
    for ch, cols in cjk_rows:
        cp = ord(ch)
        body = ", ".join("0x%02X" % b for b in cols)
        entries.append("    { 0x%04X, { %s } }," % (cp, body))
    parts.append("static const CJK16_T CJK16[%d] = {\n%s\n};\n" % (len(entries), "\n".join(entries)))
    parts.append("#define CJK16_COUNT %d\n#endif\n" % len(entries))

    text = "".join(parts)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print("写出 %s  %d 字节" % (OUT, len(text)))

    # 顺手在终端里预览几个字，肉眼确认没渲染错
    def show(cols, w, h, label):
        print("\n--- %s ---" % label)
        for y in range(h):
            line = ""
            for x in range(w):
                page, bit = y // 8, y % 8
                on = cols[page * w + x] >> bit & 1
                line += "##" if on else "  "
            print(line)

    show(ascii_rows[ord("A") - 32], W_ASCII, H_ASCII, "A")
    show(ascii_rows[ord("8") - 32], W_ASCII, H_ASCII, "8")
    show(cjk_rows[0][1], W_CJK, H_CJK, cjk_rows[0][0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
