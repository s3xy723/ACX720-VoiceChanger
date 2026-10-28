#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验「上位机 → 单片机」的模式配置包：打包端 vs 解析端。

    python tool/verify_oled_cfg.py

打包端是 tool/fpga_audio_bridge.py 里真正会写进串口的 build_cfg_packet()；
解析端按 mcu/f103_spi_mcp4922/cfg_proto.c 的状态机逐字节复刻一遍。
两边对不上 = 屏幕上的数字会错，而且错得很隐蔽（比如半音和变调比装反）。
另外还测三件事：CRC 错的包必须被拒、被噪声打断后能重新同步、字段越界要夹紧。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fpga_audio_bridge import build_cfg_packet, crc8  # noqa: E402

FSM = dict(MAGIC0=0xAA, MAGIC1=0x55, LEN=11, TYPE=1)


class Parser(object):
    """cfg_proto.c 里 cfg_parse_byte() 的逐字节复刻。"""

    def __init__(self):
        self.st = 0
        self.plen = self.ptype = self.pidx = 0
        self.buf = bytearray()

    def feed(self, b, out):
        if self.st == 0:
            if b == FSM["MAGIC0"]:
                self.st = 1
        elif self.st == 1:
            self.st = 2 if b == FSM["MAGIC1"] else (1 if b == FSM["MAGIC0"] else 0)
        elif self.st == 2:
            self.plen = b
            self.st = 3 if b == FSM["LEN"] else 0
        elif self.st == 3:
            self.ptype = b
            self.pidx = 0
            self.buf = bytearray()
            self.st = 4
        elif self.st == 4:
            self.buf.append(b)
            self.pidx += 1
            if self.pidx >= self.plen:
                self.st = 5
        elif self.st == 5:
            self.st = 0
            if self.ptype != FSM["TYPE"]:
                return False
            body = bytes([self.plen, self.ptype]) + bytes(self.buf)
            if crc8(body) != b:
                return False
            p = self.buf
            out.update(dict(
                mode=p[0], engine=p[1], dph=(p[2] - 256 if p[2] > 127 else p[2]),
                semi10=p[3] | (p[4] << 8) if (p[3] | (p[4] << 8)) < 32768
                else (p[3] | (p[4] << 8)) - 65536,
                ratio1000=p[5] | (p[6] << 8),
                vg=p[7], ag=p[8],
                g=(p[9] - 256 if p[9] > 127 else p[9]),
                src=p[10]))
            return True
        return False


def decode(pkt):
    pr = Parser()
    got = {}
    hits = 0
    for b in pkt:
        if pr.feed(b, got):
            hits += 1
    return (got if hits else None), hits


def case(name, d, want, prefix=b""):
    pkt = build_cfg_packet(d)
    if pkt is None:
        print("  FAIL %-24s 打包返回 None" % name)
        return 0, 1
    got, hits = decode(prefix + pkt)
    ok = (hits == 1 and got == want)
    print("  %-4s %-24s %s" % ("OK" if ok else "FAIL", name,
                               "" if ok else "got=%r hits=%d" % (got, hits)))
    return (1 if ok else 0), (0 if ok else 1)


def main():
    p = f = 0
    print("== 打包 → 解析 一致性 ==")
    a, b = case("变调 +4.6 半音��文件",
                dict(m="pitch", e="fpga_direct", semi=4.6, dph=19, ratio=1.2969,
                     vg=100, ag=100, g=0, src="file"),
                dict(mode=0, engine=0, dph=19, semi10=46, ratio1000=1297,
                     vg=100, ag=100, g=0, src=1))
    p += a; f += b

    a, b = case("变调 -3.2 半音（小模型）",
                dict(m="pitch", e="fpga_model", semi=-3.2, dph=-25, ratio=0.6094,
                     vg=80, ag=90, g=-6, src="live"),
                dict(mode=0, engine=3, dph=-25, semi10=-32, ratio1000=609,
                     vg=80, ag=90, g=-6, src=0))
    p += a; f += b

    a, b = case("分离 本地模型",
                dict(m="split", e="local", semi=0.0, dph=0, ratio=1.0,
                     vg=70, ag=100, g=3, src="file"),
                dict(mode=1, engine=2, dph=0, semi10=0, ratio1000=1000,
                     vg=70, ag=100, g=3, src=1))
    p += a; f += b

    a, b = case("分离 中置提取",
                dict(m="split", e="center", semi=0.0, dph=0, ratio=1.0,
                     vg=100, ag=55, g=0, src="live"),
                dict(mode=1, engine=1, dph=0, semi10=0, ratio1000=1000,
                     vg=100, ag=55, g=0, src=0))
    p += a; f += b

    a, b = case("越界要夹紧（vg=300, g=+40）",
                dict(m="pitch", e="fpga_direct", semi=0, dph=0, ratio=1.0,
                     vg=300, ag=-5, g=40, src="live"),
                dict(mode=0, engine=0, dph=0, semi10=0, ratio1000=1000,
                     vg=200, ag=0, g=12, src=0))
    p += a; f += b

    print("\n== 鲁棒性 ==")
    good = build_cfg_packet(dict(m="pitch", e="fpga_direct", semi=7.0, dph=12,
                                ratio=1.1875, vg=100, ag=100, g=0, src="file"))
    # 1) 前面垫一段音频垃圾（含大量 0xA5，模拟真实串口流）还能同步
    junk = bytes([0xA5, 0x5A, 0x07] + list(range(200, 240)))
    got, hits = decode(junk + good)
    ok = (hits == 1 and got and got["semi10"] == 70)
    print("  %-4s 噪声前缀后仍能同步        hits=%d semi10=%s" %
          ("OK" if ok else "FAIL", hits, got and got["semi10"]))
    p += ok; f += (not ok)

    # 2) CRC 被改坏必须整包丢弃
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    got2, hits2 = decode(bytes(bad))
    ok2 = (hits2 == 0 and got2 is None)
    print("  %-4s CRC 错必须拒绝            hits=%d" % ("OK" if ok2 else "FAIL", hits2))
    p += ok2; f += (not ok2)

    # 3) 截断的包不能误判
    got3, hits3 = decode(good[:-3])
    ok3 = (hits3 == 0)
    print("  %-4s 截断包必须拒绝            hits=%d" % ("OK" if ok3 else "FAIL", hits3))
    p += ok3; f += (not ok3)

    print("\n通过 %d，失败 %d" % (p, f))
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
