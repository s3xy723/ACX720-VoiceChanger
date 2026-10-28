# -*- coding: utf-8 -*-
"""
tool_check_ai.py   AI 依赖自检（给「运行上位机_AI完整版.bat」用）

打印一份人能看懂的报告，并给出退出码：
    0 = 全部就绪，可以直接启动
    1 = 缺东西，bat 会提示怎么装

运行：python tool_check_ai.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 每一项：(import 名, pip 包名, 说明, 是否必需)
ITEMS = [
    ("numpy", "numpy", "基础数组运算", True),
    ("sounddevice", "sounddevice", "实时声卡输入输出", True),
    ("soundfile", "soundfile", "WAV/FLAC/OGG 读写", True),
    ("av", "av", "M4A/AAC/WMA 解码（PyAV 内置 ffmpeg）", False),
    ("torch", "torch", "神经网络运行时（AI 分离必需）", True),
    ("demucs", "demucs", "人声/伴奏分离模型", True),
]

OK_ALL = True


def head(s):
    print("\n" + s)
    print("-" * max(len(s) * 2, 40))


def main():
    global OK_ALL
    OK_ALL = True
    print("=" * 62)
    print(" ACX720 上位机 · AI 依赖自检")
    print("=" * 62)

    head("1. Python 包")
    for name, pipname, desc, required in ITEMS:
        try:
            m = __import__(name)
            ver = getattr(m, "__version__", "?")
            print("  [OK]   %-12s %-10s %s" % (name, ver, desc))
        except Exception as e:
            tag = "[缺少]" if required else "[可选]"
            print("  %s %-12s %s  -> pip install %s"
                  % (tag, name, desc, pipname))
            if required:
                OK_ALL = False

    head("2. CUDA（决定 AI 分离跑 GPU 还是 CPU）")
    try:
        torch = __import__("torch")
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            print("  [OK]   %s" % torch.cuda.get_device_name(0))
            print("         显存 %.1f GB，计算能力 sm_%d%d"
                  % (p.total_memory / 1024.0 ** 3, *torch.cuda.get_device_capability(0)))
        else:
            print("  [提示] CUDA 不可用，AI 分离会跑 CPU（约慢 5~15 倍）")
            print("         如果有 NVIDIA 显卡，装 CUDA 版 torch：")
            print("         pip install torch torchaudio --index-url "
                  "https://download.pytorch.org/whl/cu128")
    except Exception as e:
        print("  [跳过] torch 没装，先装 torch 再看 CUDA")

    head("3. demucs 权重（htdemucs）")
    try:
        import local_ai
        if local_ai._hf_weights_cached():
            print("  [OK]   已在本地缓存，不需要联网")
        else:
            ep = local_ai.ensure_model_endpoint()
            if ep:
                print("  [提示] 本地还没缓存，会用镜像 %s 下载（约 80 MB）" % ep)
            else:
                print("  [提示] 本地还没缓存，首次运行会从 huggingface.co "
                      "下载约 80 MB")
    except Exception as e:
        print("  [跳过] %s: %s" % (type(e).__name__, e))

    head("结论")
    if OK_ALL:
        print("  全部就绪。启动后选「分离」模块 + 「计算机本地模型」引擎即可。")
        return 0
    print("  还有必需项没装好，AI 分离不可用；其他功能不受影响。")
    return 1


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        print("\n自检脚本自身出错：%s: %s" % (type(e).__name__, e))
        rc = 1
    print()
    raise SystemExit(rc)
