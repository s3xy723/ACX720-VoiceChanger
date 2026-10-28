#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 FPGA 比特流：综合 + 实现 + 写 bitstream，然后把产物复制成
vivado_prj/voice_changer.bit（烧录脚本认这个路径）。

    python tool/rebuild_bitstream.py

为什么单独有这个脚本
--------------------
手动流程是：跑 build_uart_audio.tcl → 去 impl_1 目录找 .bit → 复制改名。
漏掉最后一步的后果是"跑完了、没报错、烧进去还是旧逻辑"，非常难发现。
本脚本把复制这一步固化下来，并在复制前把旧文件改名备份。
"""
import glob
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TCL = os.path.join(HERE, "build_uart_audio.tcl")
PRJ = os.path.join(ROOT, "vivado_prj", "voice_changer")
DEST = os.path.join(ROOT, "vivado_prj", "voice_changer.bit")
LOG = os.path.join(HERE, "logs", "fpga_build.log")

VIVADO_CANDIDATES = [
    r"D:\AMDVivado\Vivado\2023.2\bin\vivado.bat",
    r"D:\AMDVivado\2026.1\Vivado\bin\vivado.bat",
    r"D:\Vivado\Vivado\2018.3\bin\vivado.bat",
    r"C:\Xilinx\Vivado\2018.3\bin\vivado.bat",
]


def find_vivado():
    for p in VIVADO_CANDIDATES:
        if os.path.exists(p):
            return p
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, "vivado.bat")
        if os.path.exists(p):
            return p
    return None


def newest_bit():
    hits = glob.glob(os.path.join(PRJ, "*.runs", "impl_1", "*.bit"))
    hits = [h for h in hits if os.path.getsize(h) > 1000000]
    return max(hits, key=os.path.getmtime) if hits else None


def main():
    viv = find_vivado()
    if viv is None:
        print("找不到 vivado.bat，请改本脚本里的 VIVADO_CANDIDATES")
        return 2
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    t0 = time.time()
    print("开始综合/实现（通常 3~8 分钟）…")
    print("日志：%s" % LOG)
    with open(LOG, "wb") as f:
        rc = subprocess.call([viv, "-mode", "batch", "-source", TCL],
                             stdout=f, stderr=subprocess.STDOUT, cwd=HERE)
    print("Vivado 退出码 %d，用时 %.1f 分钟" % (rc, (time.time() - t0) / 60.0))
    if rc != 0:
        print("构建失败，看日志里的 ERROR / CRITICAL WARNING")
        return rc

    bit = newest_bit()
    if bit is None:
        print("构建结束但没找到 .bit 产物")
        return 3
    if os.path.exists(DEST):
        bak = os.path.join(os.path.dirname(DEST),
                           "voice_changer_prev_%s.bit" %
                           time.strftime("%Y%m%d_%H%M"))
        shutil.copy2(DEST, bak)
        print("旧比特流已备份：%s" % os.path.basename(bak))
    shutil.copy2(bit, DEST)
    print("产物 %s  %.1f MB  %s" %
          (os.path.basename(bit), os.path.getsize(bit) / 1e6,
           time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(bit)))))
    print("已覆盖 %s" % DEST)
    print("下一步：tool\\烧录FPGA.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
