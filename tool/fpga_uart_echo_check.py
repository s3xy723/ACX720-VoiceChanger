#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fpga_uart_echo_check.py —— COM4(ACX720 板载 CH340) 链路自检

FPGA 侧行为（rtl/voice_changer_top.v）：
    uart_audio_rx 每收到一个 **CRC 校验通过** 的音频帧就发一次 frame_pulse，
    顶层用它驱动 uart_tx 回送 **一个字节 0xA5**。
    所以"收到 N 个 0xA5" == "FPGA 成功解析了 N 帧"，前提是排除了本地回环。

为什么要重写判据
----------------
旧版只判 `echo.count(0xA5) > 0`。问题是 0xA5 恰好是我们自己发的帧头首字节：
如果 TX/RX 被短接（线接错、某些底板直连），PC 会收到自己发出去的全部
261×N 字节，其中必然含 0xA5 —— 于是 FPGA 根本没参与也会 PASS。

新版用三条判据来区分：
    1. 基线：打开端口后先什么都不发，静听 0.5 s。有线噪声/自发回环会在这
       里现形，>0 直接判 INCONCLUSIVE。
    2. 字节数：真回显是"每帧 1 字节"，bytes_read ≈ frames_sent；
       本地回环是"每帧 261 字节"，bytes_read ≈ 261 × frames_sent。
    3. 比例：ok_a5 / frames_sent 应接近 1。

Usage:
    python fpga_uart_echo_check.py COM4
    python fpga_uart_echo_check.py COM4 --seconds 3 --baud 1500000
"""
import argparse
import os
import sys
import time

import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 从 uart_frame 取协议常量，避免与 FPGA 侧漂移；也不再连累 import sounddevice
from uart_frame import BAUD, FS_OUT, FRAME_BYTES, N_SAMPLES, build_frame  # noqa: E402
# 串口走统一入口：有 pyserial 用 pyserial，没有就退回 ctypes 直连 Win32，
# 保证离线机器上也跑得起来（串口是硬件调试第一步，不能被包缺失卡住）
from uart_port import open_serial  # noqa: E402


def _tone_frames(n_frames, freq=440.0, amp=0.25):
    """生成 n_frames 个 440 Hz 测试音帧（纯 Python，不依赖 numpy）。"""
    out = []
    seq = 0
    for i in range(n_frames):
        blk = []
        for k in range(N_SAMPLES):
            idx = i * N_SAMPLES + k
            v = int(round(amp * math.sin(2.0 * math.pi * freq * idx / FS_OUT)
                          * 32767.0))
            blk.append((max(-32768, min(32767, v)),) * 2)
        out.append(build_frame(blk, seq))
        seq = (seq + 1) & 0xFF
    return out


def main(port="COM4", baud=BAUD, seconds=3.0):
    ser = None
    try:
        ser, backend = open_serial(port, baud, timeout=0.05, write_timeout=1.0)
        print("serial backend = %s" % backend)
    except Exception as e:
        print("RESULT: FAIL  打不开 %s：%s" % (port, e))
        return 2

    try:
        # ---- 1) 基线：什么都没发，先听 0.5 s ----
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        t_end = time.perf_counter() + 0.5
        baseline = 0
        while time.perf_counter() < t_end:
            d = ser.read(4096)
            if d:
                baseline += len(d)

        # ---- 2) 发帧 + 收回显 ----
        rate = FS_OUT / N_SAMPLES                 # 帧/秒
        n_frames = int(rate * seconds)
        frames = _tone_frames(n_frames)
        print("port=%s baud=%d  frames_to_send=%d (%.1f s)"
              % (port, baud, n_frames, seconds))
        print("baseline_bytes=%d  (发数据之前收到的字节，正常应为 0)" % baseline)

        echo = bytearray()
        sent = 0
        t0 = time.perf_counter()
        next_t = t0
        for f in frames:
            ser.write(f)
            sent += 1
            # 边发边收，避免 1.5 Mbaud 下回显把驱动缓冲撑爆
            d = ser.read(4096)
            if d:
                echo += d
            next_t += 1.0 / rate
            now = time.perf_counter()
            if next_t > now:
                time.sleep(min(next_t - now, 0.005))

        # 收尾：把还在路上/驱动缓冲里的回显收干净
        t_end = time.perf_counter() + 0.4
        while time.perf_counter() < t_end:
            d = ser.read(4096)
            if not d:
                break
            echo += d

        n_a5 = echo.count(0xA5)
        n_bytes = len(echo)
        print("frames_sent=%d bytes_read=%d 0xA5=%d  (每帧回 1 字节才是真回显；"
              "本地回环会是 %d 字节/帧)" % (sent, n_bytes, n_a5, FRAME_BYTES))
        if n_bytes:
            print("echo_hex(head)=%s" % echo[:40].hex())

        # ---- 3) 判定 ----
        if baseline > 0:
            print("RESULT: INCONCLUSIVE  未发送数据就收到 %d 字节，"
                  "线路上有自发数据/回环，无法证明是 FPGA 回的" % baseline)
            return 3
        if n_bytes == 0:
            print("RESULT: FAIL  一个字节都没收到。"
                  "查：FPGA 是否已烧录含 uart_tx 回显的 bit；"
                  "USB 线是否插在板上标 USB-UART 的那个口（不是 USB2.0 口）；"
                  "波特率是否 %d。" % baud)
            return 1
        if n_bytes > 4 * sent:
            print("RESULT: FAIL  收到 %d 字节 >> 发送 %d 帧，"
                  "这是 TX/RX 短接造成的**本地回环**，不是 FPGA 回显"
                  % (n_bytes, sent))
            return 1
        ratio = n_a5 / float(max(1, sent))
        if ratio >= 0.8:
            print("RESULT: PASS  FPGA 回显 %d/%d 帧（%.0f%%）"
                  % (n_a5, sent, ratio * 100.0))
            return 0
        print("RESULT: FAIL  回显比例只有 %.0f%%（%d/%d）："
              "链路通但丢帧严重。查波特率、USB 线长度、FPGA 是否 1.5 Mbaud"
              % (ratio * 100.0, n_a5, sent))
        return 1
    finally:
        # 原来异常/Ctrl-C 时端口不关，Windows 上下次打开会"拒绝访问"
        try:
            if ser is not None:
                ser.reset_output_buffer()
                ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM4")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args()
    sys.exit(main(a.port, a.baud, a.seconds))
