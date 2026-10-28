# -*- coding: utf-8 -*-
"""age_link.py —— 把"源年龄 → 目标年龄"换算成 FPGA 的参数包并发过去。

分工（很重要，别把换算塞进 RTL）：
    Python  side：年龄 → (r, β, 气声, 微颤, 高频倾斜)  ← 可测、可调、可打印
    FPGA    side：只收 4 个定点数，做定点乘加            ← age_tint + pitch_shift

包格式与 rtl/cfg_parser.v 严格一致：
    AA 55 | cmd(01) | len(0A) | data[10] | crc_lo crc_hi
    CRC16-CCITT, init=FFFF, poly=1021，覆盖 cmd + len + data

自检：`python age_link.py`
    G1 黄金向量：RTL 仿真实测该包的 CRC = 0x9DB9，这里必须一模一样
    G2 打包 → 解包 往返一致（解包器是独立实现，不是把入参读回来）
    G3 改坏一位 → CRC 必须对不上（负对照，防止校验和恒真）
"""
import argparse
import struct
import sys

MAGIC = b"\xAA\x55"
CMD_AGE = 0x01
LEN_AGE = 10

# RTL 侧 tb_cfg_parser.v 实测：
#   cmd=01 len=0A data=[0,80,0,0,250,20,30,1,0,0] -> 9DB9
GOLDEN_DATA = bytes([0, 80, 0, 0, 250, 20, 30, 1, 0, 0])
GOLDEN_CRC = 0x9DB9


def crc16(data, c=0xFFFF):
    """CRC16-CCITT，与 rtl/cfg_parser.v 的 crc16() 逐位一致。"""
    for b in data:
        x = (c ^ (b << 8)) & 0xFFFF
        for _ in range(8):
            x = ((x << 1) ^ 0x1021) & 0xFFFF if (x & 0x8000) else (x << 1) & 0xFFFF
        c = x
    return c


def build_age_packet(beta16, dphase, tilt, breath, shim, en):
    """按 cfg_parser 的字段顺序打包。所有值都按有/无符号原样写入。"""
    data = struct.pack("<hhbBBBxx",
                       int(beta16), int(dphase), int(tilt),
                       int(breath) & 0xFF, int(shim) & 0xFF, 1 if en else 0)
    assert len(data) == LEN_AGE, len(data)
    body = bytes([CMD_AGE, LEN_AGE]) + data
    c = crc16(body)
    return MAGIC + body + bytes([c & 0xFF, (c >> 8) & 0xFF])


def parse_age_packet(buf):
    """独立解包（自检用）：返回 dict，失败返回 None。"""
    if len(buf) < 6 or buf[0:2] != MAGIC:
        return None
    cmd, ln = buf[2], buf[3]
    if cmd != CMD_AGE or ln != LEN_AGE or len(buf) < 4 + ln + 2:
        return None
    body = buf[2:4 + ln]
    got = buf[4 + ln] | (buf[5 + ln] << 8)
    if crc16(body) != got:
        return None
    d = buf[4:4 + ln]
    beta, dph, tilt = struct.unpack("<hhb", d[0:5])
    return dict(beta16=beta, dphase=dph, tilt=tilt,
                breath=d[5], shim=d[6], en=d[7] & 1)


def packet_from_ages(a_from, a_to, sex="m", strength=1.0):
    """年龄 → 参数包。换算全部走 host/age_model.py，这里只做打包。"""
    import age_model as AM
    p = AM.params(a_from, a_to, sex, strength)
    beta16 = int(round(p["beta"] * 16384.0))
    beta16 = max(-32768, min(32767, beta16))
    # pitch_shift.v 的 dphase：r = 1 + dphase/64，Q0.6，范围 ±511/64
    dphase = int(round((p["r"] - 1.0) * 64.0))
    dphase = max(-511, min(511, dphase))
    tilt = int(round(p.get("tilt_db", 0.0) * 8.0))     # Q0.3，约 ±16 dB
    tilt = max(-128, min(127, tilt))
    breath = int(round(p.get("breath", 0.0) * 255.0))
    shim = int(round(p.get("shimmer", 0.0) * 8.0))
    return build_age_packet(beta16, dphase, tilt,
                            max(0, min(255, breath)),
                            max(0, min(255, shim)), True), p


def send(packet, port=None, baud=1500000):
    """把包写进串口。没装 pyserial / 没给端口时只打印，不报错。"""
    if not port:
        return False, "未指定串口，仅生成数据包"
    try:
        import serial
    except Exception as e:
        return False, "缺少 pyserial（%r）" % (e,)
    try:
        with serial.Serial(port, baud, timeout=0.2) as sp:
            sp.write(packet)
            sp.flush()
        return True, "已发送 %d 字节到 %s@%d" % (len(packet), port, baud)
    except Exception as e:
        return False, "串口发送失败：%r" % (e,)


def _selftest():
    ok = bad = 0

    def chk(name, cond, detail=""):
        nonlocal ok, bad
        if cond:
            ok += 1
            print("  [PASS] %s %s" % (name, detail))
        else:
            bad += 1
            print("  [FAIL] %s %s" % (name, detail))

    print("age_link 自检")
    c = crc16(bytes([CMD_AGE, LEN_AGE]) + GOLDEN_DATA)
    chk("G1 黄金向量与 RTL 一致", c == GOLDEN_CRC, "python=%04X rtl=%04X" % (c, GOLDEN_CRC))

    pk = build_age_packet(20480, 0, -6, 20, 30, True)
    d = parse_age_packet(pk)
    chk("G2 打包→解包往返一致",
        d is not None and d["beta16"] == 20480 and d["tilt"] == -6
        and d["breath"] == 20 and d["shim"] == 30 and d["en"] == 1,
        "len=%d %s" % (len(pk), d))

    broken = bytearray(pk)
    broken[6] ^= 0x01
    chk("G3 改坏一位后 CRC 必须失败（负对照）",
        parse_age_packet(bytes(broken)) is None,
        "解析结果=None")

    print("\n  PASS = %d  FAIL = %d" % (ok, bad))
    return 0 if bad == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="a_from", type=int, default=None)
    ap.add_argument("--to", dest="a_to", type=int, default=None)
    ap.add_argument("--sex", default="m", choices=["m", "f"])
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--port", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest or a.a_from is None:
        return _selftest()
    pk, p = packet_from_ages(a.a_from, a.a_to, a.sex, a.strength)
    print("参数：r=%.3f beta=%.3f breath=%.2f shimmer=%.2f tilt=%+.1f dB"
          % (p["r"], p["beta"], p.get("breath", 0.0), p.get("shimmer", 0.0),
             p.get("tilt_db", 0.0)))
    print("数据包（%d 字节）：%s" % (len(pk), " ".join("%02X" % b for b in pk)))
    ok, msg = send(pk, a.port)
    print(msg)
    return 0 if ok or a.port is None else 1


if __name__ == "__main__":
    sys.exit(main())
