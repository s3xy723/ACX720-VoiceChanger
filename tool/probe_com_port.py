#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""识别一个 COM 口上挂的到底是 STM32 的调试串口，还是 ACX720 板载 CH340（FPGA）。

为什么需要它
------------
机器上往往只有一个 CH340。STM32 的 PA9(115200) 和 FPGA 的板载 CH340(1.5Mbaud)
用同一个 USB-UART 芯片型号，设备管理器里看不出区别；脚本里写死的 COM4 也可能
因为插拔顺序漂移成 COM5。发音频发到"其实是 PA9 调试口"的那个口上，
现象是"帧全发了、板子一点反应没有"，极难定位。

判据（两条独立的，互不依赖）
----------------------------
1. 115200 被动听 2.5 s：STM32 固件每 500 ms 打一行
      mode=TEST(440Hz) words/s=0 fs=0 ovr=0
   看到 `mode=` 且能解析出 words/fs/ovr → 这是 **STM32 调试口**。
2. 1.5 Mbaud 发一个合法帧（uart_frame 协议）：FPGA 每收到一帧回一个 0xA5。
   收到 ≥1 个 0xA5 → 这是 **FPGA 音频口**。

用法：
    python tool/probe_com_port.py COM5
    python tool/probe_com_port.py COM4 COM5     # 可以同时给多个
退出码：0 = 至少识别出一个；1 = 都没识别出。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uart_frame import BAUD, N_SAMPLES, build_frame  # noqa: E402

try:
    import serial
except Exception:
    print("需要 pyserial：pip install pyserial")
    raise


def probe_stm32(port, secs=2.5):
    """115200 被动听。返回 (是STM32?, 原始文本片段)。"""
    try:
        s = serial.Serial(port, 115200, bytesize=8, parity="N",
                          stopbits=1, timeout=0.2)
    except Exception as e:
        return False, "打不开：%r" % (e,)
    try:
        buf = b""
        t0 = time.time()
        while time.time() - t0 < secs:
            buf += s.read(256)
    finally:
        s.close()
    txt = buf.decode("ascii", errors="replace")
    # 必须是固件自己的状态行，不能是随便什么 ASCII 噪声
    return ("mode=" in txt), txt


def probe_fpga(port):
    """1.5Mbaud 发 1 帧，看有没有 0xA5 回显。返回 (是FPGA?, 描述)。"""
    try:
        s = serial.Serial(port, BAUD, bytesize=8, parity="N",
                          stopbits=1, timeout=0.3, write_timeout=2.0)
    except Exception as e:
        return False, "打不开：%r" % (e,)
    try:
        s.reset_input_buffer()
        samples = [(0, 0)] * N_SAMPLES
        s.write(build_frame(samples, 0))
        time.sleep(0.15)
        r = s.read(64)
    finally:
        s.close()
    n_a5 = sum(1 for b in r if b == 0xA5)
    return n_a5 > 0, "回读 %d 字节，其中 0xA5 %d 个" % (len(r), n_a5)


def main():
    ports = sys.argv[1:] or ["COM4", "COM5", "COM3"]
    any_ok = False
    for p in ports:
        print("\n===== %s =====" % p)
        ok32, txt = probe_stm32(p)
        if ok32:
            any_ok = True
            print("  [115200] → STM32 调试口（PA9）")
            for line in txt.replace("\r", "").split("\n"):
                if "mode=" in line:
                    print("     " + line.strip())
        else:
            print("  [115200] 不是 STM32 调试口：%s"
                  % (txt[:60].replace("\n", " ") or "(无数据)"))

        okfp, desc = probe_fpga(p)
        if okfp:
            any_ok = True
            print("  [1500000] → ACX720 板载 CH340（FPGA 音频口） %s" % desc)
        else:
            print("  [1500000] 不是 FPGA 音频口：%s" % desc)

        if not ok32 and not okfp:
            print("  → 未识别。可能没插、被别的程序占用，或波特率/接线不对。")
    print("\nRESULT: %s" % ("OK" if any_ok else "NONE"))
    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())
