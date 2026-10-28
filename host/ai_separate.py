# -*- coding: utf-8 -*-
"""
ai_separate.py —— 可选的人声提取前端（demucs）

为什么要它
----------
纯 DSP 的中置提取只能拿到"左右两边共有的那部分"：人声、贝斯、底鼓、
居中的钢琴全在里面。合成基准上 demucs 的人声提取是 **+19 dB SI-SDR**，
中置提取做不到这个量级 —— 不是参数没调好，是信息量不够。

分工
----
    demucs            负责"从整首歌里把人声挖出来"（难的那一半）
    vocal_split.py    负责"把挖出来的人声按男女分到左右"（剩下的一半）

这样伴奏由 demucs 的 no_vocals 轨原样保留，人声只被搬到指定声道。

关于依赖
--------
`demucs` + `torch` 装完大约 300 MB～2 GB，**不会打进 EXE**。
所以它是"有就用、没有就自动退回纯 DSP"的可选项：
界面上勾选框会自动置灰并说明原因，不会让程序崩。
"""

import os
import threading

import numpy as np

MODEL_NAME = "htdemucs"

_model = None
_model_lock = threading.Lock()
_last_error = None


def available():
    """demucs 装没装（只看能不能 import，不触发模型下载）。

    ★★ 必须先让 local_ai 把本机 torch 挂进 sys.path，再 import torch。
       打包版（轻量 onedir）刻意**没有**把 torch 打进包，torch 是运行时从
       用户级 site-packages 挂载进去的。旧实现这里直接 __import__("torch")，
       而 host_app 是在 import 阶段就调用本函数的 —— 那时候还没挂载，必然
       报 ModuleNotFoundError，于是 host_app.HAVE_AI 永远为 False，
       「分离 × 计算机本地模型」每次都**静默退回纯 DSP 中置提取**，
       demucs 一次都没真正跑过。表现就是"人声里别的乐器全在"。

    ★ 挂载只在第一次真正需要时发生（会 import torch，几秒）。所以调用方
       不要把它放在启动路径上，应该在"确实要跑分离"之前调。
    """
    global _last_error
    try:
        import local_ai
        local_ai.ensure_local_runtime()
        torch = __import__("torch")  # noqa: F401
        import demucs  # noqa: F401
        return True, ""
    except Exception as e:
        _last_error = repr(e)
        return False, _last_error


def _get_model(name=MODEL_NAME):
    global _model
    with _model_lock:
        if _model is None:
            from demucs.pretrained import get_model
            m = get_model(name)
            m.eval()
            _model = m
    return _model


def model_samplerate():
    try:
        return int(getattr(_get_model(), "samplerate", 44100))
    except Exception:
        return 44100


def separate(x, sr, progress=None, device="auto", quality="std"):
    """Delegate to local_ai.separate, which has the tuned demucs presets.

    quality: fast / std / high.  local_ai maps it to shifts/overlap/segment
    （high 档还会换成 htdemucs_ft 四模型集成），做 sinc 重采样和 CUDA OOM
    回退；只保留一份实现，避免文件路径和实时路径慢慢走偏。

    """
    import local_ai
    voc, acc, info = local_ai.separate(
        x, sr, device=device, quality=quality, progress=progress)
    return np.ascontiguousarray(voc), np.ascontiguousarray(acc)


def separate_mono_vocal(x, sr, progress=None, quality="std"):
    """只人声，返回单声道 (n,)。"""
    voc, acc = separate(x, sr, progress, quality=quality)
    return 0.5 * (voc[:, 0] + voc[:, 1]), acc
