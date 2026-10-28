#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 OLED 会显示的内容画成 PNG，不用接线也能先看版式。

    python tool/preview_oled.py                 # 三种典型状态各出一张
    python tool/preview_oled.py --m pitch --e fpga_model --semi 7 --dph 12

版式逻辑与 mcu/.../main.c 的 oled_refresh() 一一对应（改那边记得改这边）；
字模直接读自动生成的 oled_font.h，所以看到的就是屏幕上会出现的像素。
"""
import argparse
import os
import re
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_H = os.path.join(os.path.dirname(HERE), "mcu", "f103_spi_mcp4922", "oled_font.h")
OUT_DIR = os.path.join(HERE, "logs")

CJK_CODE = {"变": 0x53D8, "调": 0x8C03, "分": 0x5206, "离": 0x79BB}


def load_fonts():
    txt = open(FONT_H, encoding="utf-8").read()
    body = txt.split("FONT8X16[96][16] = {")[1].split("};")[0]
    rows = re.findall(r"\{([^}]*)\}", body)
    ascii_tab = [[int(x, 16) for x in r.replace("0x", "").split(",") if x.strip()]
                 for r in rows]
    cjk = {}
    for m in re.finditer(r"\{ 0x([0-9A-F]{4}), \{([^}]*)\} \}", txt):
        cjk[int(m.group(1), 16)] = [int(x, 16) for x in
                                    m.group(2).replace("0x", "").split(",") if x.strip()]
    return ascii_tab, cjk


class Screen(object):
    def __init__(self):
        self.img = Image.new("1", (128, 64), 0)

    def text(self, page, x, s):
        ascii_tab, _ = FONTS
        px = self.img.load()
        for ch in s:
            o = ord(ch)
            if o < 32 or o > 127:
                o = ord("?")
            idx = o - 32
            for i in range(8):
                lo = ascii_tab[idx][i]
                hi = ascii_tab[idx][8 + i]
                for bit in range(8):
                    if lo >> bit & 1:
                        px[x + i, page * 8 + bit] = 1
                    if hi >> bit & 1:
                        px[x + i, page * 8 + 8 + bit] = 1
            x += 8

    def cjk(self, page, x, ch):
        _, tab = FONTS
        m = tab.get(CJK_CODE[ch])
        px = self.img.load()
        for i in range(16):
            lo = m[i] if m else 0x81
            hi = m[16 + i] if m else 0x81
            for bit in range(8):
                if lo >> bit & 1:
                    px[x + i, page * 8 + bit] = 1
                if hi >> bit & 1:
                    px[x + i, page * 8 + 8 + bit] = 1
        x += 16


def i_str(v):
    return ("+" if v >= 0 else "-") + str(abs(v))


def f_str(v, dec):
    d = 10 ** dec
    return "%d.%0*d" % (v // d, dec, v % d)


def draw(path, m="pitch", e="fpga_direct", semi10=46, dph=19, ratio100=130,
         vg=100, ag=100, g=0, link=True):
    sc = Screen()
    sc.text(0, 0, "ACX720")
    sc.text(0, 96, "LINK" if link else "  --")
    if not link:
        sc.text(2, 0, "WAIT HOST")
        sc.text(4, 0, "PA3 = USART2")
        sc.text(6, 0, "1.5M cfg")
    elif m == "pitch":
        sc.cjk(2, 0, "变")
        sc.cjk(2, 16, "调")
        sc.text(2, 36, f_str(ratio100, 3) + "x")
        sc.text(4, 0, ("MODEL" if e == "fpga_model" else "FPGA") +
                " SEMI" + i_str(semi10 // 10) + "." + str(abs(semi10 % 10)))
        sc.text(6, 0, "DPH" + i_str(dph) + "  G" + i_str(g) + "dB")
    else:
        sc.cjk(2, 0, "分")
        sc.cjk(2, 16, "离")
        sc.text(2, 36, "LOCAL" if e == "local" else "CENTER")
        sc.text(4, 0, "VOC%d%%  ACC%d%%" % (vg, ag))
        sc.text(6, 0, "GAIN" + i_str(g) + "dB")
    os.makedirs(OUT_DIR, exist_ok=True)
    # 放大 4 倍 + 加一圈边框，方便直接看
    big = sc.img.resize((128 * 4, 64 * 4), Image.NEAREST).convert("RGB")
    big.save(path)
    print("写出 %s" % path)
    return path


FONTS = None


def main():
    global FONTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", default=None, help="pitch / split")
    ap.add_argument("--e", default=None, help="fpga_direct / fpga_model / center / local")
    ap.add_argument("--semi", type=float, default=4.6)
    ap.add_argument("--ratio", type=float, default=None)
    ap.add_argument("--vg", type=int, default=100)
    ap.add_argument("--ag", type=int, default=100)
    ap.add_argument("--g", type=int, default=0)
    a = ap.parse_args()
    FONTS = load_fonts()

    if a.m:
        dph = int(round((a.semi) * 64.0 / 12.0))
        ratio = a.ratio if a.ratio else 1.0 + dph / 64.0
        out = os.path.join(OUT_DIR, "oled_%s_%s.png" % (a.m, a.e or "x"))
        draw(out, a.m, a.e or "fpga_direct", int(round(a.semi * 10)), dph,
             int(round(ratio * 1000)), a.vg, a.ag, a.g)
        return 0

    draw(os.path.join(OUT_DIR, "oled_1_pitch_fpga.png"),
         "pitch", "fpga_direct", 46, 19, 1297, 100, 100, 0)
    draw(os.path.join(OUT_DIR, "oled_2_pitch_model.png"),
         "pitch", "fpga_model", -32, -25, 609, 80, 90, -6)
    draw(os.path.join(OUT_DIR, "oled_3_split_local.png"),
         "split", "local", 0, 0, 1000, 70, 100, 3)
    draw(os.path.join(OUT_DIR, "oled_4_wait.png"), link=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
