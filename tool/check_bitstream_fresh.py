#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 vivado_prj/voice_changer.bit 是不是比 rtl/*.v 旧。

    python tool/check_bitstream_fresh.py            # 0=新，1=旧，2=缺文件

为什么要有这个检查（2026-10-23 踩过的坑）
----------------------------------------
板子上烧的是**比特流的快照**，不是源代码。RTL 改完不重新综合、直接把
旧 .bit 烧进去，表现是"链路全通、数据也在动，但喇叭里全是杂音"：

    spi_mirror_tx.v 在 2026-10-20 把 CS̄ 从"每字一个脉冲"改成
    "每帧一个脉冲"（左右声道靠帧起点同步）。而 voice_changer.bit 是
    改动**之前**综合的 —— 于是 STM32 的 EXTI4 每个字都触发一次，
    g_side 永远被清 0，几乎从不写 DAC：DAC 长期停在旧样值上，
    偶发刷新时拿到的是错位的左右字 → 噪声。

这类问题在"数据速率正常、CRC 全对、寄存器全对"的假象下极难定位。
所以烧录前先比时间戳，比任何事后排查都便宜。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIT = os.path.join(ROOT, "vivado_prj", "voice_changer.bit")
RTL = os.path.join(ROOT, "rtl")
XDC = os.path.join(ROOT, "constr", "voice_changer.xdc")


def main():
    if not os.path.exists(BIT):
        print("找不到比特流：%s" % BIT)
        return 2
    t_bit = os.path.getmtime(BIT)
    newest, newest_t = None, 0.0
    for d, exts in ((RTL, ".v"), (os.path.dirname(XDC), ".xdc")):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(exts):
                continue
            p = os.path.join(d, f)
            t = os.path.getmtime(p)
            if t > newest_t:
                newest, newest_t = p, t

    print("比特流  %s  %s" %
          (os.path.basename(BIT),
           __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").localtime(t_bit))))
    if newest:
        print("最新 RTL %s  %s" %
              (os.path.basename(newest),
               __import__("time").strftime("%Y-%m-%d %H:%M",
                                           __import__("time").localtime(newest_t))))
    if newest and newest_t > t_bit + 1.0:
        dt = (newest_t - t_bit) / 3600.0
        print("")
        print("!! 比特流比 RTL 旧 %.1f 小时 —— 板子里跑的不是这份源码。" % dt)
        print("!! 现在烧会得到：链路全通、数据在动、但声音是杂音/错位。")
        print("!! 先重新构建：tool\\重建FPGA.bat")
        return 1
    print("OK：比特流不比 RTL 旧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
