#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""往 FPGA 灌**已知** PCM（不经 CABLE、不经重采样），用来隔离数字链路。

    python tool/probe_inject_pcm.py --const 0x1234 --secs 3
    python tool/probe_inject_pcm.py --sine 1000 --amp 0.6 --secs 3

配合 `mcu/f103_spi_mcp4922/capture.hex`（-DCAPTURE 编译的固件）用：
灌进去什么，从 MCU 的 g_cap 里读出来就该是什么。逐样本比对即可判定
FPGA→SPI→MCU 这段有没有丢字/错字。
"""
import argparse
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from uart_frame import BAUD, FS_OUT, N_SAMPLES, build_frame  # noqa: E402
from uart_port import open_serial  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--const", default=None, help="恒定 16 位值，如 0x1234")
    ap.add_argument("--sine", type=float, default=None, help="正弦频率")
    ap.add_argument("--amp", type=float, default=0.6)
    ap.add_argument("--secs", type=float, default=3.0)
    a = ap.parse_args()

    port = a.port
    if port is None:
        from serial.tools import list_ports
        hits = [p.device for p in list_ports.comports()
                if "CH340" in (p.description or "") or "1A86" in (p.hwid or "").upper()]
        port = hits[0] if hits else "COM4"

    if a.const is not None:
        c = int(a.const, 0) & 0xFFFF
        mode = "const 0x%04X" % c
    elif a.sine is not None:
        mode = "sine %.0f Hz amp %.2f" % (a.sine, a.amp)
    else:
        print("要么 --const 要么 --sine")
        return 2

    ser, backend = open_serial(port, BAUD)
    n_frames = int(round(a.secs * FS_OUT / N_SAMPLES))
    print("port=%s baud=%d frames=%d 后端=%s  %s" % (port, BAUD, n_frames, backend, mode))

    phase = 0.0
    step = 2.0 * math.pi * (a.sine or 0.0) / FS_OUT
    t0 = time.perf_counter()
    nxt = t0
    for i in range(n_frames):
        if a.const is not None:
            l = r = c
        else:
            v = int(round(a.amp * 32767 * math.sin(phase)))
            phase += step
            l = r = v & 0xFFFF
        pcm = np.tile(np.array([l & 0xFFFF, r & 0xFFFF], dtype=np.uint16), (N_SAMPLES, 1))
        ser.write(build_frame(pcm, i & 0xFF))
        nxt += N_SAMPLES / FS_OUT
        d = nxt - time.perf_counter()
        if d > 0:
            time.sleep(min(d, 0.02))
    dur = time.perf_counter() - t0
    print("发完 %d 帧，用时 %.2f s，折合 %.1f 帧/秒（目标 %.1f）" %
          (n_frames, dur, n_frames / dur, FS_OUT / N_SAMPLES))
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
