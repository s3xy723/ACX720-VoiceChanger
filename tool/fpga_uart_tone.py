#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send a 440 Hz test tone to the FPGA over COM4 (1.5 Mbaud).

This is the shortest end-to-end check:
    PC -> COM4 -> FPGA UART -> (voice_proc) -> SPI -> STM32 -> DAC -> speaker

Usage:
    python fpga_uart_tone.py --port COM4 --seconds 10
"""
import argparse
import math
import os
import sys
import time

import numpy as np
import serial

# 从 uart_frame 取协议常量：原来从 fpga_audio_bridge 导入，会连带 import
# sounddevice，缺音频栈时报的错和串口完全无关，很误导排查方向。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uart_frame import BAUD, FS_OUT, N_SAMPLES, build_frame  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("--amp", type=float, default=0.25)
    args = ap.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N",
                            stopbits=1, timeout=0.1, write_timeout=1.0)
    except Exception as e:
        print("FAIL 打不开 %s：%s" % (args.port, e))
        return 2
    print("OPEN %s @ %d" % (args.port, args.baud))

    try:
        total = int(args.seconds * FS_OUT)
        sent = 0
        seq = 0
        t0 = time.perf_counter()
        next_t = t0
        while sent < total:
            n = np.arange(sent, sent + N_SAMPLES, dtype=np.float64)
            s = (args.amp * np.sin(2.0 * math.pi * args.freq * n / FS_OUT)
                 * 32767.0).astype(np.int16)
            frame = np.stack([s, s], axis=1)
            ser.write(build_frame(frame, seq))
            seq = (seq + 1) & 0xFF
            sent += N_SAMPLES

            next_t += N_SAMPLES / FS_OUT
            now = time.perf_counter()
            if next_t > now:
                time.sleep(min(next_t - now, 0.02))
            elif now - next_t > 0.2:
                next_t = now
        print("DONE frames=%d elapsed=%.2fs"
              % (sent // N_SAMPLES, time.perf_counter() - t0))
        return 0
    except Exception as e:
        print("FAIL 发送中断：%r" % (e,))
        return 1
    finally:
        # 这个脚本就是让人 Ctrl-C 用的，异常路径也必须关端口，
        # 否则 Windows 上后续脚本再打开会"拒绝访问"
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())


