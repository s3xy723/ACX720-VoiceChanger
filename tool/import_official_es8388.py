# -*- coding: utf-8 -*-
"""
import_official_es8388.py

把芯路恒(小梅哥) ACX720-V3 官方例程 ch35_CoreCourse_GHRD_I2S_RX 里的
ES8388 配置与 I2C 控制器复制进本工程 rtl/，并把 GBK 注释转成 UTF-8。

来源文件（官方例程，已带可下载 .bit，证明能跑通）：
    ES8388_Init.v        -> rtl/es8388_init.v
    ES8388_init_table.v  -> rtl/es8388_init_table.v
    I2C_Init_Dev.v       -> rtl/i2c_init_dev.v
    i2c_control.v        -> rtl/i2c_control.v
    i2c_bit_shift.v      -> rtl/i2c_bit_shift.v

用法：python tool/import_official_es8388.py <解压后的例程目录>
"""
import os
import shutil
import sys

SRC = [
    ("ES8388_Init.v",       "es8388_init.v"),
    ("ES8388_init_table.v", "es8388_init_table.v"),
    ("I2C_Init_Dev.v",      "i2c_init_dev.v"),
    ("i2c_control.v",       "i2c_control.v"),
    ("i2c_bit_shift.v",     "i2c_bit_shift.v"),
]

# 官方文件里的中文注释在 GBK 下写的，转码后替换掉几个关键乱码词没有意义，
# 直接做 GBK -> UTF-8 即可
ENCODINGS = ["gbk", "gb18030", "cp936", "latin-1"]


def to_utf8(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def main():
    if len(sys.argv) < 2:
        print("usage: python import_official_es8388.py <ch35_dir>")
        return 1
    src_root = sys.argv[1]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst = os.path.join(here, "rtl")
    for name, newname in SRC:
        p = os.path.join(src_root, name)
        if not os.path.isfile(p):
            print("MISSING:", p)
            continue
        txt = to_utf8(open(p, "rb").read())
        out = os.path.join(dst, newname)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        print("OK  %-22s -> %s  (%d chars)" % (name, newname, len(txt)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
