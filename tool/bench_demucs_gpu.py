# -*- coding: utf-8 -*-
"""
bench_demucs_gpu.py   demucs 分离的真实耗时实测（CUDA vs CPU）

机器上 torch / demucs 都装好之后，真正要确认的是三件事：
  1. **模型有没有真的跑到 GPU 上**（不是"装了 CUDA 但还在用 CPU 算"）；
  2. 一首歌要多久 —— 这决定实时模式能不能用、文件模式要等多久；
  3. 显存占用多少，会不会 OOM。

素材用 tool/_demucs_voc.npy / _demucs_acc.npy 重混成"歌曲"
（这两个是之前 verify_local_ai.py 跑出来的真实分离结果缓存，
 混回去再分离一次，构成闭环 —— 至少能证明通路与还原自洽性）。

运行：python tool/bench_demucs_gpu.py [--seconds N] [--device cuda|cpu|auto]
"""

import argparse
import os
import sys
import time

import numpy as np

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOP)
sys.path.insert(0, os.path.join(TOP, "host"))

VOC_NPY = os.path.join(TOP, "tool", "_demucs_voc.npy")
ACC_NPY = os.path.join(TOP, "tool", "_demucs_acc.npy")
SR = 44100


def load_mix(seconds):
    """人声 + 伴奏 -> 立体声混音。没有缓存就现场合成一段。"""
    if os.path.exists(VOC_NPY) and os.path.exists(ACC_NPY):
        voc = np.load(VOC_NPY)
        acc = np.load(ACC_NPY)
        # ★ 缓存的 npy 是 (样本数, 声道) —— 与 separate() 期望的输入一致。
        #   早期版本写成 voc.shape[-1]，取到的是声道数 2，切出来只剩 2 个
        #   样本，后面 reshape 时会去申请 1.4 TB 内存。
        n = min(voc.shape[0], acc.shape[0], int(seconds * SR))
        mix = (voc[:n] + acc[:n]).astype(np.float32)
        if mix.ndim == 1:
            mix = np.repeat(mix[:, None], 2, axis=1)
        return np.ascontiguousarray(mix), "缓存的人声/伴奏重混"
    # 没有缓存：合成一段（谐波人声 + 低频伴奏），只用于测通路与耗时
    n = int(seconds * SR)
    t = np.arange(n) / float(SR)
    f0 = 220.0
    voc = sum(0.5 / (k + 1) * np.sin(2 * np.pi * f0 * (k + 1) * t) for k in range(8))
    voc *= 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)          # 颤音让它更像人声
    acc = 0.3 * np.sin(2 * np.pi * 80.0 * t) + 0.2 * np.random.RandomState(0).randn(n)
    mix = np.stack([voc + acc, voc + acc], axis=0)
    return np.ascontiguousarray(mix.T).astype(np.float32), "合成素材"


def rms(x):
    return float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0


def mem_mb():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.max_memory_allocated() / (1024.0 ** 2)
    except Exception:
        pass
    return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    args = ap.parse_args()

    import torch
    import local_ai

    print("=" * 72)
    print(" demucs 分离实测：GPU 有没有真用上 / 一首歌要多久")
    print("=" * 72)
    print("torch      %s" % torch.__version__)
    print("CUDA 可用  %s" % torch.cuda.is_available())
    if torch.cuda.is_available():
        print("显卡       %s（%.1f GB，sm_%d%d）"
              % (torch.cuda.get_device_name(0),
                 torch.cuda.get_device_properties(0).total_memory / 1024.0 ** 3,
                 *torch.cuda.get_device_capability(0)))
    ok, why = local_ai.demucs_ready()
    print("demucs 就绪 %s %s" % (ok, why))
    print("环境摘要   %s" % local_ai.summary_text())
    if not ok:
        print("\n[FAIL] demucs 不可用：%s" % why)
        return 1

    mix, src = load_mix(args.seconds)
    dur = mix.shape[0] / float(SR)
    print("\n素材       %s，%.1f 秒 @ %d Hz，%d 声道"
          % (src, dur, SR, mix.shape[1]))

    results = []
    for dev in (["cuda", "cpu"] if args.device == "auto" else [args.device]):
        if dev == "cuda" and not torch.cuda.is_available():
            print("\n[cuda] 跳过：CUDA 不可用")
            continue
        try:
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
            t0 = time.time()
            voc, acc, info = local_ai.separate(mix, SR, device=dev,
                                               quality=local_ai.QUALITY_STD)
            dt = time.time() - t0
        except Exception as e:
            print("\n[%s] 失败：%s: %s" % (dev, type(e).__name__, e))
            results.append((dev, None, None, None))
            continue

        real = dur / dt if dt > 0 else 0.0
        results.append((dev, dt, info, mem_mb()))
        print("\n[%s]" % dev)
        print("  返回 device  %s" % info.get("device"))
        print("  模型         %s" % info.get("model", "?"))
        print("  耗时         %.2f s   （%.1f 倍实时）" % (dt, real))
        print("  显存峰值     %.0f MB" % (mem_mb() if dev == "cuda" else 0))
        print("  人声 RMS     %.4f   伴奏 RMS %.4f   输入 RMS %.4f"
              % (rms(voc), rms(acc), rms(mix)))
        print("  形状         voc %s  acc %s" % (voc.shape, acc.shape))

    print("\n" + "=" * 72)
    gpu = next((r for r in results if r[0] == "cuda" and r[1]), None)
    cpu = next((r for r in results if r[0] == "cpu" and r[1]), None)
    if gpu and cpu:
        print("CUDA 相对 CPU 加速 %.1f 倍" % (cpu[1] / gpu[1]))
    if gpu:
        print("结论：GPU 路径可用，%.1f 秒音频耗时 %.2f s"
              % (dur, gpu[1]))
    elif cpu:
        print("结论：只有 CPU 路径可用，%.1f 秒音频耗时 %.2f s" % (dur, cpu[1]))
    else:
        print("结论：[FAIL] 两条路径都没跑通")
        return 1
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
