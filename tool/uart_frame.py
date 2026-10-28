#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uart_frame.py —— PC<->FPGA 音频帧协议的**唯一事实源**（零依赖）

为什么要有这个文件
------------------
帧格式原来在三个地方各写一份：

    tool/fpga_audio_bridge.py      （上位机桥）
    rtl/uart_audio_rx.v            （FPGA 解析）
    sim_src/tb_uart_audio_rx.v     （仿真）

CRC 多项式、帧头、采样点数、波特率各写各的。目前三处恰好一致，但改任一侧
（比如把每帧采样数 64 提到 128）另一侧**不会报错**，只会变成"上位机显示
PASS、板子全是 CRC 错"这种极难定位的现象。

现在 Python 侧全部从这里取常量；RTL 侧在参数声明处注明与此文件对应。

帧格式（小端）
--------------
    A5 5A | seq | 64 x (L_lo L_hi R_lo R_hi) | crc_lo crc_hi
    CRC16-CCITT：init 0xFFFF，poly 0x1021，覆盖范围 = seq + 256 字节载荷

必须由 verify_uart_frame.py 做固定向量交叉校验。
"""

# ---------------- 链路参数（与 rtl/uart_audio_rx.v 的参数一致） ----------------
FS_OUT = 24414.0625          # 50 MHz / 2048，FPGA 侧音频采样率
N_SAMPLES = 64               # 每帧立体声采样点数
BAUD = 1_500_000             # 8N1

# ---------------- 帧结构 ----------------
SYNC0 = 0xA5
SYNC1 = 0x5A
PAYLOAD_BYTES = N_SAMPLES * 4          # 256
FRAME_BYTES = 2 + 1 + PAYLOAD_BYTES + 2   # 261

# ---------------- CRC16-CCITT ----------------
CRC_INIT = 0xFFFF
CRC_POLY = 0x1021


def _make_table():
    tbl = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ CRC_POLY) & 0xFFFF if (c & 0x8000) else ((c << 1) & 0xFFFF)
        tbl.append(c)
    return tbl


_CRC_TABLE = _make_table()


def crc16(data):
    """CRC16-CCITT (init 0xFFFF, poly 0x1021)，返回 int。"""
    c = CRC_INIT
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC_TABLE[((c >> 8) ^ b) & 0xFF]
    return c


def build_frame(samples, seq):
    """samples: (N_SAMPLES, 2) 个 int -> 261 字节帧（小端）。

    刻意用 struct 而不是 numpy：链路自检脚本要能在没装 numpy 的机器上跑
    （本机就是这个情况），协议层不该拖一个科学计算栈进来。
    """
    import struct

    flat = []
    for row in samples:
        for v in (int(row[0]), int(row[1])):
            # 先按二进制补码折回有符号范围，负数才不会被 struct 拒收
            flat.append(((v + 32768) & 0xFFFF) - 32768)
    if len(flat) != N_SAMPLES * 2:
        raise ValueError("样本数必须是 %d，实际 %d"
                         % (N_SAMPLES, len(flat) // 2))
    payload = struct.pack("<%dh" % (N_SAMPLES * 2), *flat)
    body = bytes([seq & 0xFF]) + payload
    c = crc16(body)
    return bytes([SYNC0, SYNC1]) + body + struct.pack("<H", c)


def parse_stream(buf):
    """从字节流里按帧同步解析出完整帧，返回 (frames, stats)。

    frames: list of dict(seq=, payload=bytes, crc_ok=bool)
    stats:  dict(sync=, good=, crc_bad=, short=)
    用于回环/回显检查：只数 0xA5 的个数是不可靠的（见 fpga_uart_echo_check）。
    """
    frames = []
    st = dict(sync=0, good=0, crc_bad=0, short=0)
    i = 0
    n = len(buf)
    while i + FRAME_BYTES <= n:
        if buf[i] != SYNC0 or buf[i + 1] != SYNC1:
            i += 1
            continue
        body = buf[i + 2:i + 2 + 1 + PAYLOAD_BYTES]
        got = buf[i + 2 + 1 + PAYLOAD_BYTES] | (buf[i + 3 + 1 + PAYLOAD_BYTES] << 8)
        st["sync"] += 1
        ok = (crc16(body) == got)
        st["good" if ok else "crc_bad"] += 1
        frames.append(dict(seq=body[0], payload=body[1:], crc_ok=ok))
        i += FRAME_BYTES
    st["short"] = n - i
    return frames, st


# ---------------- 固定测试向量（跨 Python / RTL 交叉校验用） ----------------
def _selftest():
    """内置固定向量：改了协议就必须同步改这里。"""
    x = [(1000 + i, -1000 - i) for i in range(N_SAMPLES)]
    f = build_frame(x, 0)
    assert len(f) == FRAME_BYTES, len(f)
    assert f[0] == SYNC0 and f[1] == SYNC1
    assert f[2] == 0
    assert crc16(f[2:2 + 1 + PAYLOAD_BYTES]) == (f[-2] | (f[-1] << 8))
    fr, stt = parse_stream(f)
    assert stt["good"] == 1 and stt["crc_bad"] == 0, stt
    # 破坏一个字节应被 CRC 抓到
    bad = bytearray(f)
    bad[10] ^= 0xFF
    _, stt2 = parse_stream(bytes(bad))
    assert stt2["crc_bad"] == 1, stt2
    return True


if __name__ == "__main__":
    _selftest()
    print("uart_frame selftest PASS  FS_OUT=%s N=%d BAUD=%d FRAME=%dB"
          % (FS_OUT, N_SAMPLES, BAUD, FRAME_BYTES))
