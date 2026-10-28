#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fpga_audio_bridge.py

Capture the Windows playback endpoint "CABLE Input (VB-Audio Virtual Cable)"
from its loopback side "CABLE Output", resample to 24.414 kHz / 16-bit /
stereo, and send framed PCM to the ACX720 FPGA over its USB-UART (CH340).

Frame format (must match rtl/uart_audio_rx.v):
    A5 5A | seq | 64 x (L_lo L_hi R_lo R_hi) | crc_lo crc_hi
    CRC16-CCITT, init 0xFFFF, poly 0x1021, over seq + 256 payload bytes.

Usage:
    python fpga_audio_bridge.py --port COM4
    python fpga_audio_bridge.py --list
    python fpga_audio_bridge.py --dry-run

Dependencies: sounddevice numpy pyserial
"""

import argparse
import json
import os
import queue
import sys
import threading
import time

import numpy as np

# 帧协议常量统一从 uart_frame 取：原来 CRC/帧头/采样点数在本文件、RTL、
# 仿真里各写一份，改一处不报错，只会在板子上变成"全是 CRC 错"。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uart_frame import (BAUD, FS_OUT, FRAME_BYTES, N_SAMPLES,  # noqa: E402
                        build_frame, crc16)

try:
    import sounddevice as sd
except Exception:
    print("missing sounddevice: pip install sounddevice")
    raise

try:
    import serial
except Exception:
    print("missing pyserial: pip install pyserial")
    raise


# 开播前先垫进 FPGA FIFO 的帧数。8 帧 = 512 点 ≈ 21 ms，用来盖住
# CH340 的 USB 突发延迟（默认 latency timer 16 ms）。详见 sender() 里的注释。
PREFILL_FRAMES = 8
# 延迟上限：缓冲超过 12 帧就砍到 2 帧（约 31 ms → 5 ms），避免延迟越拖越长
BUF_MAX_FRAMES = 12


# ---------------------------------------------------------------- resampler
class StreamResampler:
    """44.1/48 kHz -> 24.414 kHz: 63-tap low-pass + linear interpolation."""

    def __init__(self, fs_in, fs_out=FS_OUT, taps=63):
        self.fs_in = float(fs_in)
        self.fs_out = float(fs_out)
        self.ratio = self.fs_in / self.fs_out
        self.taps = taps
        fc = 0.5 * min(1.0, self.fs_out / self.fs_in) * 0.90
        n = np.arange(-(taps // 2), taps // 2 + 1, dtype=np.float64)
        h = 2.0 * fc * np.sinc(2.0 * fc * n)
        h *= np.kaiser(taps, 5.0)
        h /= np.sum(h)
        self.h = h.astype(np.float64)
        self.tail = np.zeros((taps - 1, 2), dtype=np.float64)
        self.buf = np.zeros((0, 2), dtype=np.float64)
        self.pos = 0.0

    def reset(self):
        self.tail[:] = 0.0
        self.buf = np.zeros((0, 2), dtype=np.float64)
        self.pos = 0.0

    def push(self, x):
        """x: (n,2) float -> (m,2) float at fs_out"""
        x = np.asarray(x, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != 2:
            x = np.repeat(x.reshape(-1, 1), 2, axis=1)
        orig_len = x.shape[0]
        if orig_len == 0:
            # 空块：原来会走进 np.convolve，当 len(xpad)=62 < taps=63 时
            # numpy 会交换两个输入，返回长度 2 的结果，赋给 0 行的 y 直接
            # ValueError。任何复用这个类的场景都会踩到。
            return np.zeros((0, 2), dtype=np.float64)
        if x.shape[0] < self.taps:
            # 输入比滤波器还短：补齐到 taps，保证卷积输出长度 == 输入长度
            pad = np.zeros((self.taps - x.shape[0], 2), dtype=np.float64)
            x = np.vstack([x, pad])

        xpad = np.vstack([self.tail, x])
        y = np.empty_like(x)
        for ch in range(2):
            y[:, ch] = np.convolve(xpad[:, ch], self.h, mode="valid")
        self.tail = xpad[-(self.taps - 1):, :].copy()
        # 只把真实输入的卷积结果放进缓冲：上面为凑 taps 补的零不能变成
        # 真实音频样本，否则会在短块处插进一小段静音、把时间轴拉长。
        y = y[:orig_len]

        self.buf = np.vstack([self.buf, y])
        if self.pos >= len(self.buf) - 1:
            return np.zeros((0, 2), dtype=np.float64)

        out = []
        while self.pos + 1.0 < len(self.buf):
            i = int(self.pos)
            f = self.pos - i
            out.append(self.buf[i] * (1.0 - f) + self.buf[i + 1] * f)
            self.pos += self.ratio

        if self.pos > 1024:
            drop = int(self.pos) - 2
            self.buf = self.buf[drop:].copy()
            self.pos -= drop

        if not out:
            return np.zeros((0, 2), dtype=np.float64)
        return np.asarray(out, dtype=np.float64)


# ---------------------------------------------------------------- 配置包
# 上位机把「当前模式」写进 tool/logs/mode.json，本桥读到变化就插一个 16 字节
# 配置包到串口里（夹在音频帧之间）。FPGA 的帧状态机只认 A5 5A，会跳过它；
# STM32 的 USART2(PA3) 也挂在这根线上，它认 AA 55 并据此刷新 OLED。
CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "mode.json")
CFG_MAGIC0, CFG_MAGIC1 = 0xAA, 0x55
CFG_TYPE_MODE = 1


def crc8(data):
    """CRC-8 poly 0x07 init 0x00（和 mcu/.../main.c 里的 crc8 必须一致）"""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if (c & 0x80) else ((c << 1) & 0xFF)
    return c


def build_cfg_packet(d):
    """mode.json -> 16 字节配置包；字段含义见 mcu/.../main.c 的注释"""
    try:
        mode = 1 if d.get("m") == "split" else 0
        eng = {"fpga_direct": 0, "fpga_model": 3,
               "center": 1, "local": 2}.get(d.get("e"), 0)
        dph = max(-64, min(63, int(round(float(d.get("dph", 0))))))
        semi10 = max(-32768, min(32767, int(round(float(d.get("semi", 0.0)) * 10))))
        ratio1000 = max(0, min(65535, int(round(float(d.get("ratio", 1.0)) * 1000))))
        vg = max(0, min(200, int(d.get("vg", 100))))
        ag = max(0, min(200, int(d.get("ag", 100))))
        gdb = max(-60, min(12, int(d.get("g", 0))))
        src = 1 if d.get("src") == "file" else 0
        sw = max(0, min(255, int(d.get("sw", 0))))
    except Exception:
        return None
    pl = bytes([mode & 0xFF, eng & 0xFF, dph & 0xFF,
                semi10 & 0xFF, (semi10 >> 8) & 0xFF,
                ratio1000 & 0xFF, (ratio1000 >> 8) & 0xFF,
                vg, ag, gdb & 0xFF, src])
    body = bytes([len(pl), CFG_TYPE_MODE]) + pl
    return bytes([CFG_MAGIC0, CFG_MAGIC1]) + body + bytes([crc8(body)])


# ---------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, port, baud=BAUD, dry_run=False):
        self.port = port
        self.baud = baud
        self.dry_run = dry_run
        self.ser = None
        self.q = queue.Queue(maxsize=40)
        self.running = True
        self.seq = 0
        self.frames = 0
        self.dropped = 0
        # dropped 由音频回调线程和发送线程同时 +=，无锁会丢更新
        self._drop_lock = threading.Lock()
        # 致命错误标志：原来串口写失败后进程会以**退出码 0** 结束，
        # 外面（比如一键启动的 bat）完全看不出链路已经死了。
        self.fatal = threading.Event()
        self.fatal_msg = ""
        self.last_log = time.time()
        self.capture_rms = 0.0
        self.resampler = None
        self.stream = None
        self._pending = np.zeros((0, 2), dtype=np.float64)
        self._status_warned = False
        self.cfg_pkt = None          # 待发送的配置包（None = 没有新的）
        self.cfg_stamp = None        # mode.json 的 (mtime, size)，用来判断变没变
        self.cfg_count = 0
        self.cfg_last = None         # 最后一次发出的包（心跳重发用）
        self.cfg_last_t = 0.0

    @staticmethod
    def find_cable_input():
        best = None
        for idx, d in enumerate(sd.query_devices()):
            if "CABLE Output" not in d["name"]:
                continue
            if d["max_input_channels"] < 2:
                continue
            host = sd.query_hostapis(d["hostapi"])["name"]
            score = 2 if "WASAPI" in host else 1
            if best is None or score > best[0]:
                best = (score, idx, d)
        return best[1] if best else None

    def open_serial(self):
        if self.dry_run:
            return
        self.ser = serial.Serial(
            self.port, self.baud, bytesize=8, parity="N", stopbits=1,
            timeout=0.1, write_timeout=1.0)
        print("serial %s open, %d 8N1" % (self.port, self.baud))

    def callback(self, indata, frames, tinfo, status):
        if status and not self._status_warned:
            print("capture status:", status)
            self._status_warned = True
        x = np.array(indata, dtype=np.float64, copy=True)
        self.capture_rms = float(np.sqrt(np.mean(x * x)))
        y = self.resampler.push(x)
        if len(y) == 0:
            return

        if len(self._pending):
            pending = np.vstack([self._pending, y])
        else:
            pending = y

        n = len(pending) // N_SAMPLES * N_SAMPLES
        for i in range(0, n, N_SAMPLES):
            block = pending[i:i + N_SAMPLES]
            try:
                self.q.put_nowait(block)
            except queue.Full:
                with self._drop_lock:
                    self.dropped += 1
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.q.put_nowait(block)
                except queue.Full:
                    pass
        self._pending = pending[n:].copy()

    def _poll_cfg(self):
        """看一眼 mode.json 有没有变；变了就排队一个配置包。

        用 (mtime, size) 判断而不是每次读文件：这个函数在发送线程里每帧
        都会调，读盘太浪费。上位机写文件用的是原子替换，所以不会读到半个 JSON。
        """
        try:
            st = os.stat(CFG_FILE)
        except Exception:
            return
        key = (int(st.st_mtime_ns), st.st_size)
        if key == self.cfg_stamp:
            return
        self.cfg_stamp = key
        try:
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        pkt = build_cfg_packet(d)
        if pkt:
            self.cfg_pkt = pkt
            self.cfg_count += 1
            print("config -> %s/%s ratio=%.3f semi=%.2f (第 %d 次)"
                  % (d.get("m"), d.get("e"), float(d.get("ratio", 1.0)),
                     float(d.get("semi", 0.0)), self.cfg_count))

    def sender(self):
        buf = np.zeros((0, 2), dtype=np.float64)
        next_t = time.perf_counter()
        started = False
        while self.running:
            try:
                block = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            buf = np.vstack([buf, block])
            if len(buf) > BUF_MAX_FRAMES * N_SAMPLES:
                # 积压太多说明串口发得比采得慢：砍到 2 块（跳掉中间那段，
                # 听感是"卡一下"，好过延迟越拖越长）。
                buf = buf[-2 * N_SAMPLES:]
                with self._drop_lock:
                    self.dropped += 1

            if not started:
                if len(buf) < PREFILL_FRAMES * N_SAMPLES:
                    continue
                started = True
                # ★ 2026-10-23：先灌满一段当"水位"，再开始按节拍发。
                #   原来只等 2 帧（128 点 = 5.2 ms）就开跑，而 FPGA 那边
                #   的 FIFO 是**从 0 开始**的：PC 侧发出速率与 FPGA 消耗速率
                #   名义上相等（都是 24414.0625），所以水位永远回不来，
                #   一直贴着 0 —— CH340 的 USB 延迟（默认 16 ms 定时器）
                #   一攒够就一次性吐好几帧，中间那 16 ms FPGA 无数据可读，
                #   只能"重复上一个样值"（uart_audio_rx.v 的 hold last），
                #   听感就是**只在有声音时**才听得见的"沙沙/断续/毛糙"。
                #   预灌 8 帧 = 512 点 ≈ 21 ms 水位，正好盖住 USB 的突发延迟；
                #   代价是播放延迟多 21 ms（人耳不觉）。
                next_t = time.perf_counter()
                pre_left = PREFILL_FRAMES

            self._poll_cfg()
            # 心跳重发：内容不变也每秒补发一次。否则单片机那边 3 秒收不到包
            # 就会显示 WAIT HOST（它的判断是"最近 3 秒有没有包"），
            # 而且 MCU 复位/重烧之后也能自己恢复，不用去动一下界面。
            if (self.cfg_pkt is None and self.cfg_last is not None
                    and time.time() - self.cfg_last_t >= 1.0):
                self.cfg_pkt = self.cfg_last
            if self.cfg_pkt is not None and self.ser is not None:
                try:
                    self.ser.write(self.cfg_pkt)
                    self.cfg_last = self.cfg_pkt
                    self.cfg_last_t = time.time()
                    self.cfg_pkt = None
                    # 给 UART 留一小段空闲：MCU 那边的 IDLE 中断靠这个间隔
                    # 把配置包切成独立一段，否则会被算进音频帧的字节流里
                    time.sleep(0.003)
                except Exception as exc:
                    self.fatal_msg = "串口写失败：%s" % exc
                    self.fatal.set()
                    self.running = False
                    break

            while len(buf) >= N_SAMPLES:
                chunk = buf[:N_SAMPLES]
                buf = buf[N_SAMPLES:]
                pcm = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                frame = build_frame(pcm, self.seq)
                self.seq = (self.seq + 1) & 0xFF
                if self.ser is not None:
                    try:
                        self.ser.write(frame)
                    except Exception as exc:
                        # 标致命错误：让 run() 以非 0 退出，而不是伪装成功
                        self.fatal_msg = "串口写失败：%s" % exc
                        self.fatal.set()
                        self.running = False
                        break
                self.frames += 1

                if pre_left > 0:
                    pre_left -= 1
                    continue            # 预灌阶段：不睡眠，尽快把水位垫起来

                next_t += N_SAMPLES / FS_OUT
                now = time.perf_counter()
                if next_t > now:
                    time.sleep(min(next_t - now, 0.02))
                elif now - next_t > 0.2:
                    next_t = now

            if time.time() - self.last_log >= 2.0:
                self.last_log = time.time()
                print("frames=%d q=%d dropped=%d rms=%.4f" %
                      (self.frames, self.q.qsize(), self.dropped, self.capture_rms))

    def run(self):
        idx = self.find_cable_input()
        if idx is None:
            print("CABLE Output (VB-Audio Virtual Cable) not found.")
            print("Install VB-CABLE and select 'CABLE Input' as Windows playback device.")
            return 2
        d = sd.query_devices(idx)
        host = sd.query_hostapis(d["hostapi"])["name"]
        fs_in = int(d["default_samplerate"])
        print("capture: [%d] %s (%s), %d Hz, %d ch" %
              (idx, d["name"], host, fs_in, d["max_input_channels"]))
        print("target: %.1f Hz, 16-bit, stereo, %d samples/frame" %
              (FS_OUT, N_SAMPLES))

        self.resampler = StreamResampler(fs_in, FS_OUT)
        self.open_serial()

        th = threading.Thread(target=self.sender, daemon=True)
        th.start()
        try:
            self.stream = sd.InputStream(
                device=idx, channels=2, samplerate=fs_in,
                blocksize=max(256, int(fs_in * 0.01)),
                dtype="float32", callback=self.callback)
            self.stream.start()
            print("bridge running. Ctrl+C to stop.")
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("stopping...")
        finally:
            if self.fatal.is_set():
                print("FATAL:", self.fatal_msg)
            self.running = False
            try:
                if self.stream is not None:
                    self.stream.stop()
                    self.stream.close()
            except Exception:
                pass
            try:
                if self.ser is not None:
                    self.ser.close()
            except Exception:
                pass
            th.join(timeout=1.0)
        # 串口挂掉时不能用 0 退出：调用方（启动 bat / 验收脚本）会以为正常
        return 1 if self.fatal.is_set() else 0


def find_ch340():
    """机器上唯一的 CH340 串口号；找不到返回 None。

    CH340 的串口号会随插拔顺序漂移（10-21 是 COM4，10-22 变成 COM5）。
    写死一个号的表现是"桥接程序起不来"或者"帧全发了、FPGA 一点反应没有"，
    很难一眼看出是串口号的问题。机器上只有一块 CH340 时直接认它。
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return None
    hits = []
    for p in list_ports.comports():
        blob = "%s %s %s" % (p.device, p.description or "", p.hwid or "")
        if "1A86" in blob.upper() or "CH340" in blob.upper():
            hits.append(p.device)
    return hits[0] if len(hits) == 1 else None


def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] or d["max_output_channels"]:
            host = sd.query_hostapis(d["hostapi"])["name"]
            print("%2d  IN %d OUT %d  %-45s  [%s]" %
                  (i, d["max_input_channels"], d["max_output_channels"],
                   d["name"], host))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None,
                    help="FPGA 的 CH340 串口号。不给则自动找机器上唯一的 CH340")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.list:
        list_devices()
        return 0

    port = args.port
    if port is None:
        port = find_ch340()
        if port is None:
            print("没找到唯一的 CH340，回退到 COM4。"
                  "若打不开请用 --port COMx 指定（设备管理器里看）。")
            port = "COM4"
        else:
            print("自动识别到 CH340：%s" % port)

    if not args.dry_run:
        # 提示语放 Python 里而不是启动 bat 里：bat 一旦带中文，遇到非 936
        # 代码页（或系统开了 UTF-8 全局模式）就会显示成乱码。
        print("=" * 60)
        print(" FPGA 声卡桥")
        print("   CABLE Input - CH340 - FPGA UART - STM32 - DAC - 音响")
        print()
        print(" 用法：")
        print("   1. 上位机里把「输出设备」选成")
        print("        CABLE Input (VB-Audio Virtual Cable)   [Windows WASAPI]")
        print("   2. 本窗口保持开着 —— 关掉就等于断开到 FPGA 的音频")
        print("   3. 板子每次上电都要重烧一次 bit（tool\\烧录FPGA.bat）")
        print("=" * 60)
        print()

    try:
        return Bridge(port, args.baud, args.dry_run).run()
    except Exception as e:
        # 串口打不开是最常见的失败，给一句能直接照做的提示，别丢一串 traceback
        print("桥接启动失败：%r" % (e,))
        print("检查：① FPGA 板子是否上电  ② 串口号对不对（--port）"
              "  ③ 有没有别的程序占着这个 COM 口")
        return 1


if __name__ == "__main__":
    sys.exit(main())


