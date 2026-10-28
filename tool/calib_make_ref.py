#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""calib_make_ref.py —— 用真实歌曲生成 AI 分离参考（FPGA 算法校准的 ground truth）

为什么需要这一步：
  阶段二已经证明，**合成素材上算不出有意义的人声 SI-SDR**（demucs 把合成音
  判进 other 轨，vocals 能量占比 0.0%）。要量化"FPGA 的 M/S + 梳状掩蔽算法
  离 AI 差多远"，必须先有一段真人唱歌的 AI 分离结果当基准。

  这里不再用合成信号，直接拿真实歌曲跑 htdemucs，把 (人声, 伴奏) 存成 WAV，
  后面 tool/calib_fpga_sep.py 反复读它做参数扫描，不用每次都重跑模型。

用法：
    python tool/calib_make_ref.py                       # 默认用 QQFile 那首歌
    python tool/calib_make_ref.py --clip 20:200         # 只取 20s~200s
"""
from __future__ import print_function

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOP, "host"))

DEFAULT_SONG = r"D:/QQFile/老狼&王婧_想把我唱给你听.flac"
OUT_DIR = os.path.join(TOP, "work", "calib")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", default=DEFAULT_SONG)
    ap.add_argument("--clip", default="", help="秒区间，如 20:200；空 = 整首")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--quality", default="std", choices=["fast", "std", "high"])
    args = ap.parse_args()

    import soundfile as sf
    import local_ai

    if not os.path.isfile(args.song):
        print("找不到歌曲：%s" % args.song)
        return 1

    info = sf.info(args.song)
    x, sr = sf.read(args.song, dtype="float32", always_2d=True)
    print("歌曲：%s" % os.path.basename(args.song))
    print("  %d Hz / %d ch / %.1f s" % (info.samplerate, info.channels,
                                        len(x) / float(sr)))
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)

    if args.clip:
        a, b = args.clip.split(":")
        i0 = int(float(a) * sr)
        i1 = int(float(b) * sr) if b else len(x)
        x = x[i0:i1]
        print("  截取 %.1f ~ %.1f s（%.1f s）"
              % (i0 / float(sr), i1 / float(sr), len(x) / float(sr)))

    # 峰值归一化到 -1 dBFS：demucs 内部也会按 std 归一化，这里只是让
    # 后面 VocalSplitter 的 gate_db 门限有个稳定的参照
    peak = float(np.max(np.abs(x)))
    if peak > 0:
        x = (x / peak * 0.89).astype(np.float32)

    if not os.path.isdir(args.out):
        os.makedirs(args.out)

    t0 = time.time()
    print("跑 htdemucs（%s，%s 档）…" % (args.device, args.quality))
    voc, acc, meta = local_ai.separate(x, sr, device=args.device,
                                       quality=args.quality,
                                       progress=lambda s: print("  " + s))
    dt = time.time() - t0
    print("推理耗时 %.1f s（%.2f× 实时）" % (dt, dt / (len(x) / float(sr))))

    voc = np.asarray(voc, dtype=np.float32)
    acc = np.asarray(acc, dtype=np.float32)
    n = min(len(x), len(voc), len(acc))
    mix = x[:n]

    # 能量守恒核对：AI 的两轨加起来能不能还原输入
    e_sum = float(np.sum((voc[:n].astype(np.float64)
                          + acc[:n].astype(np.float64)) ** 2))
    e_mix = float(np.sum(mix.astype(np.float64) ** 2))
    ev = float(np.sum(voc[:n].astype(np.float64) ** 2))
    print("  能量守恒 (人声+伴奏)/输入 = %.4f" % (e_sum / max(e_mix, 1e-20)))
    print("  人声能量占比 = %.1f%%" % (100.0 * ev / max(e_mix, 1e-20)))
    print("  模型参数：%s" % meta)

    tag = "clip" if args.clip else "full"
    p_mix = os.path.join(args.out, "ref_%s_mix.wav" % tag)
    p_v = os.path.join(args.out, "ref_%s_vocal.wav" % tag)
    p_a = os.path.join(args.out, "ref_%s_acc.wav" % tag)
    sf.write(p_mix, mix, sr, subtype="FLOAT")
    sf.write(p_v, voc[:n], sr, subtype="FLOAT")
    sf.write(p_a, acc[:n], sr, subtype="FLOAT")
    print("已保存：")
    for p in (p_mix, p_v, p_a):
        print("  %s (%.1f MB)" % (p, os.path.getsize(p) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
