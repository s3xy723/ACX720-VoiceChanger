# -*- coding: utf-8 -*-
"""
probe_torch_dlls.py   探测 demucs 真实推理时**到底加载了哪些** torch DLL

torch cu128 全套 4.2 GB，直接打包成 EXE 不现实（单文件会 4 GB+，
启动还要解压几分钟）。但里面的一大半是 cuSOLVER / cuSPARSE / cuDNN
预编译引擎这类"某些算子才用得上"的东西，demucs（conv + transformer +LSTM）
大概率一个都不碰。

这个脚本跑一次真实的 demucs 推理，然后用 psutil 列出进程实际加载的模块，
输出：
    已加载        —— 必须打包
    未加载(>1MB)  —— 可以排除（排除后必须重跑一次验证）

运行：python tool/probe_torch_dlls.py
"""

import os
import sys

import numpy as np
import psutil

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOP)
sys.path.insert(0, os.path.join(TOP, "host"))
sys.path.insert(0, os.path.join(TOP, "tool"))

SR = 44100


def run_inference():
    import local_ai
    import torch
    n = int(6 * SR)
    rs = np.random.RandomState(0)
    t = np.arange(n) / float(SR)
    x = (0.3 * np.sin(2 * np.pi * 220 * t) + 0.2 * rs.randn(n)).astype(np.float32)
    x = np.ascontiguousarray(np.stack([x, x], axis=1))
    print("跑一次真实推理（6 秒素材，cuda）…", flush=True)
    voc, acc, info = local_ai.separate(x, SR, device="cuda",
                                       quality=local_ai.QUALITY_STD)
    print("  完成，device = %s" % info.get("device"), flush=True)
    # 再跑一次 CPU 分支不必要；这里只要把 CUDA 路径的算子都摸一遍
    return torch


def main():
    torch = run_inference()
    torch_dir = os.path.dirname(torch.__file__)
    lib_dir = os.path.join(torch_dir, "lib")

    p = psutil.Process(os.getpid())
    loaded = set()
    for m in p.memory_maps():
        loaded.add(os.path.normcase(os.path.abspath(m.path)))

    print("\n" + "=" * 74)
    print(" torch/lib 里的 DLL 加载情况")
    print("=" * 74)
    used, unused = [], []
    for f in sorted(os.listdir(lib_dir), key=lambda s: s.lower()):
        fp = os.path.join(lib_dir, f)
        if not os.path.isfile(fp):
            continue
        mb = os.path.getsize(fp) / (1024.0 ** 2)
        (used if os.path.normcase(os.path.abspath(fp)) in loaded else unused).append((f, mb))

    su = sum(m for _, m in used)
    sn = sum(m for _, m in unused)
    print("\n已加载（%d 个，合计 %.0f MB）—— 必须打包" % (len(used), su))
    for f, mb in sorted(used, key=lambda x: -x[1])[:20]:
        print("  %8.1f MB  %s" % (mb, f))

    print("\n未加载（%d 个，合计 %.0f MB）—— 可以排除" % (len(unused), sn))
    for f, mb in sorted(unused, key=lambda x: -x[1])[:25]:
        print("  %8.1f MB  %s" % (mb, f))

    # 顺带看看 nvidia / torchaudio 的情况
    print("\n" + "=" * 74)
    for mod in ("torchaudio",):
        try:
            m = __import__(mod)
            d = os.path.dirname(m.__file__)
            tot = 0.0
            hit = 0
            for root, _, files in os.walk(d):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        s = os.path.getsize(fp)
                    except OSError:
                        continue
                    tot += s
                    if os.path.normcase(os.path.abspath(fp)) in loaded:
                        hit += s
            print("%s：总 %.0f MB，已加载 %.0f MB" % (mod, tot / 1e6, hit / 1e6))
        except Exception as e:
            print("%s：%s" % (mod, e))

    print("\n建议：把上面「未加载」里 >50 MB 的 DLL 用 PyInstaller 的")
    print("      --exclude-module 之外的手段排除（见 build_exe_ai.bat）。")
    print("      排除后**必须**重跑 tool/e2e_ai_split.py 验证仍然可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
