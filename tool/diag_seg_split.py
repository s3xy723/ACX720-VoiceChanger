#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：人声 SI-SDR 为什么会随输入长度剧烈跳变（-4 dB ~ +22 dB）？

怀疑对象：demucs 的 split=True 分段/重叠相加。htdemucs 自带 segment=7.8 s，
当输入**长于** segment 时才会切成多段做 OLA；短于就是一整段直接推理。
如果分段边界处的 OLA 对稳态谐波信号有明显损伤，就能解释"8 s 的 48k 通路
（重采样后 7.35 s，不分段）+22 dB，而 8 s 的 44.1k 通路（8 s，分 2 段）
-1.9 dB"这个 24 dB 的怪现象。

跑法：同一段 truth，只在 44.1 kHz 下改输入长度（覆盖"不分段 / 分 2 段
/ 分 3 段"三种情形），看 SI-SDR 的台阶出现在哪里、出现在 segment 边界上还是
随机抖动。
"""
from __future__ import print_function

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOP, "host"))
sys.path.insert(0, HERE)

import local_ai                       # noqa: E402
from verify_sep_samplerate import make_truth, si_sdr  # noqa: E402


def main():
    sr = 44100
    lens = [5.0, 6.0, 7.0, 7.5, 7.7, 8.0, 9.0, 12.0, 16.0, 24.0]
    print("htdemucs 自带 segment = %.2f s（超过就会 split + OLA）"
          % (local_ai._model_segment(local_ai._get_model(
              local_ai.DEMUCS_MODEL, "cpu")) or 0.0))
    print()
    print("  输入长度   人声 SI-SDR   伴奏 SI-SDR   能量守恒")
    print("  " + "-" * 58)
    for secs in lens:
        n = int(round(secs * sr))
        ref_v, ref_a, mix = make_truth(n, sr)
        voc, acc, info = local_ai.separate(mix, sr, device="cpu")
        er = (float(np.sum(voc.astype(np.float64) ** 2))
              + float(np.sum(acc.astype(np.float64) ** 2))) / \
             float(np.sum(mix.astype(np.float64) ** 2))
        print("  %5.1f s     %+8.2f dB   %+8.2f dB    %.3f"
              % (secs, si_sdr(voc, ref_v), si_sdr(acc, ref_a), er))
    return 0


if __name__ == "__main__":
    sys.exit(main())
