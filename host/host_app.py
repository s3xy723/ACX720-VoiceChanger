# -*- coding: utf-8 -*-
"""
host_app.py  ——  ACX720 变声器上位机

功能
----
  实时模式   麦克风 -> 效果链 -> 扬声器/耳机   （对着麦克风说话，立刻听到变声）
  文件模式   音频文件 -> 效果链 -> 新音频文件   （可保存并回放）

操作逻辑是「模块 × 引擎 × 输入」三层，界面上就是三级单选：

    模块（做什么）      引擎（谁算）                      输入
    ├─ 变调            ├─ FPGA 直算（时域延迟线）         ├─ 实时变声
    │                  └─ FPGA 内部小模型（频域提质）     └─ 文件变声
    └─ 分离            ├─ 中置提取（纯 DSP，左人声/右伴奏）
                       └─ 计算机本地模型（demucs，左人声/右伴奏）

  四种组合 × 两种输入全部可用（实时走 dsp.EngineChain.process_stream，
  文件走各自的质量最优实现），没有哪一种被禁用。

效果链与 rtl/voice_proc.v 逐比特对齐（见 dsp_core.py），
所以「变调 × FPGA 直算」调好的参数可以直接换算成板子上的拨码开关值
（界面上有显示）；其余三种组合板上没有，只在上位机侧有效。

面板构成
--------
  左   参数控制面板（模块 / 引擎 / 输入 三级单选 + 当前组合用得到的参数）
  右上 四路波形：输入 L/R + 输出 L/R，四路分开显示（可切回频谱对照）
  右中 两个工作模式（实时变声 / 文件变声）
  右下 状态校验指示灯板：设备、采样率、信号、削波、缓冲、DSP、文件、板档位

依赖：Python 3 + numpy + sounddevice + tkinter
      （demucs / torch 可选，缺了「计算机本地模型」会明说并降级为直通）
"""

import glob
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
from tkinter import filedialog, messagebox, ttk

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ==================================================================== 磁盘日志
# ★ 2026-10-23：窗口版 EXE 没有控制台，Tk 回调里的异常原来只往 stderr 打
#   （等于扔进黑洞），用户看到的是"点了没反应""弹个看不懂的框"，事后无从定位。
#   这里把所有东西都落一份到磁盘：
#       每条界面日志、每个对话框的标题+正文、每个未捕获异常（含 traceback）
#   →  %LOCALAPPDATA%\ACX720VoiceChanger\logs\app-YYYYMMDD.log
#   出问题只要把这一个文件发过来就够了，不用再靠截图猜。
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or
                       os.path.expanduser("~"), "ACX720VoiceChanger", "logs")
_LOG_FH = None
_LOG_PATH = ""
_LOG_TRIES = []          # 每个候选目录的尝试结果，--diag 会把它打出来


def _log_candidates():
    """日志目录候选，按优先级：LOCALAPPDATA → 程序所在目录 → 临时目录。

    ★ 只用 LOCALAPPDATA 一处是不够的：实测有环境（打包成 exe 后、或带
      重定向/受限令牌启动时）往 %LOCALAPPDATA% 写会静默失败，日志一行都
      没有，排障又回到"看不见"。多存一份到 exe 自己旁边总不会错。
    """
    out = [LOG_DIR]
    try:
        if getattr(sys, "frozen", False):
            out.append(os.path.join(os.path.dirname(sys.executable), "logs"))
        else:
            out.append(os.path.join(_app_dir(), "..", "logs"))
    except Exception:
        pass
    import tempfile
    out.append(os.path.join(tempfile.gettempdir(), "ACX720VoiceChanger", "logs"))
    return out


def log_path():
    return _LOG_PATH or os.path.join(LOG_DIR, "app-%s.log" % time.strftime("%Y%m%d"))


def file_log(msg):
    """写一行到磁盘日志。任何异常都吞掉 —— 日志不能反过来把程序搞挂。"""
    global _LOG_FH, _LOG_PATH
    try:
        if _LOG_FH is None:
            name = "app-%s.log" % time.strftime("%Y%m%d")
            for d in _log_candidates():
                try:
                    os.makedirs(d, exist_ok=True)
                    p = os.path.join(d, name)
                    fh = open(p, "a", encoding="utf-8", errors="replace")
                except Exception as e:
                    _LOG_TRIES.append((d, "FAIL %r" % (e,)))
                    continue
                _LOG_TRIES.append((d, "OK"))
                _LOG_FH, _LOG_PATH = fh, p
                break
            if _LOG_FH is None:
                return
            _LOG_FH.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            try:
                _LOG_FH.write(_run_identity() + "\n")
            except Exception:
                pass
            try:
                _LOG_FH.write("cwd=%s\n" % os.getcwd())
            except Exception:
                pass
            _LOG_FH.write("日志候选：%s\n" % _LOG_TRIES)
            _LOG_FH.flush()
        _LOG_FH.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        _LOG_FH.flush()
    except Exception:
        pass


def _wrap_messagebox():
    """所有对话框先记进日志，再照常弹。import 时就装好，构造期的报错也漏不掉。"""
    for name in ("showerror", "showwarning", "showinfo"):
        orig = getattr(messagebox, name, None)
        if orig is None:
            continue

        def make(orig=orig, name=name):
            def wrapped(title=None, message=None, **kw):
                file_log("DIALOG[%s] %s | %s" %
                         (name, title, str(message).replace("\n", " ⏎ ")))
                return orig(title, message, **kw)
            return wrapped

        setattr(messagebox, name, make())


def _excepthook(exc, val, tb):
    import traceback
    file_log("UNCAUGHT\n" + "".join(traceback.format_exception(exc, val, tb)))
    try:
        sys.__excepthook__(exc, val, tb)
    except Exception:
        pass


try:
    _wrap_messagebox()
    sys.excepthook = _excepthook
except Exception:
    pass


# ★ demucs 的权重要从 huggingface.co 拉，国内经常被掐（实测 SSL 握手阶段
#   就失败，demucs 会重试 5 次再抛错，界面上就是"AI 分离一直转圈"）。
#   这个引导必须放在**任何可能 import huggingface_hub 的代码之前**：
#   HF_ENDPOINT 是 huggingface_hub 在 import 时读进常量的，设晚了不生效。
#   权重已在本地缓存时它什么都不做 —— 不联网、不耗时。
try:
    import local_ai
    local_ai.ensure_model_endpoint()
except Exception:
    pass

import audio_io
import dsp_core as dsp
import player as playermod
import spectrum as specmod
import theme
from status_panel import (BUSY, ERR, IDLE, OK, WARN, LedBoard, legend)

try:
    import ai_separate
    # ★ 这里**不能**在 import 阶段就判定。
    #   打包版没带 torch，它是运行时由 local_ai 挂进 sys.path 的；import 阶段
    #   判定必然失败，而旧代码把结果存进 HAVE_AI 之后永不刷新，于是"分离 ×
    #   计算机本地模型"每次都被静默降级成纯 DSP 中置提取（demucs 一次没跑过）。
    #   改成：启动时只记"未检测"，真正要跑分离前由 _ai_ready() 现查并缓存。
    HAVE_AI, AI_WHY = False, "启动时未检测（第一次跑分离前会现查）"
except Exception as _e:           # demucs/torch 没装也要能正常启动
    ai_separate = None
    HAVE_AI, AI_WHY = False, repr(_e)

# 真正能用 demucs 吗？只缓存**成功**结果：失败不缓存，这样用户后来装好
# torch / demucs 不用重启程序，下次处理时会自动重新判定。
_AI_READY = {"ok": False, "checked": False, "why": ""}


def _ai_ready():
    """(可用?, 原因)。第一次调用会挂载本机 torch（几秒），之后走上缓存。"""
    if ai_separate is None:
        return False, AI_WHY or "ai_separate 不可用"
    if _AI_READY["ok"]:
        return True, ""
    try:
        ok, why = ai_separate.available()
    except Exception as e:                 # 任何异常都不许把处理流程带崩
        ok, why = False, "%s: %s" % (type(e).__name__, e)
    _AI_READY["checked"] = True
    _AI_READY["why"] = "" if ok else why
    if ok:
        _AI_READY["ok"] = True
        return True, ""
    return False, why

# 本地 AI 处理（mode == MODE_AI_LOCAL）。local_ai 只依赖 numpy，一定 import
# 得进来；真正的重依赖（torch / demucs）由它内部的 probe() 按需探测，
# 缺什么就在界面上置灰并说明原因，不允许因为缺依赖而崩。
try:
    import local_ai
    HAVE_LOCAL_AI = True
    LOCAL_AI_WHY = ""
except Exception as _e:
    local_ai = None
    HAVE_LOCAL_AI = False
    LOCAL_AI_WHY = repr(_e)

# 界面上要用到的下拉项。local_ai 只依赖 numpy（本程序本来就需要），理论上
# 不可能 import 失败；真失败了也要能开窗口，只是这个模式整体置灰。
_AI_QNAMES = ([n for _, n in local_ai.QUALITY_NAMES] if local_ai
              else ["标准（本地 AI 模块不可用）"])
_AI_VNAMES = ([n for _, n in local_ai.VOICE_NAMES] if local_ai
              else ["不可用"])

try:
    import sounddevice as sd
    HAVE_SD = True
except Exception:
    sd = None
    HAVE_SD = False


SR_CHOICES = [44100, 48000]
BLOCK_CHOICES = [256, 512, 1024, 2048]

# ---------------------------------------------------------------- 缓存与巡检
# 目标是"开着放几天也不出问题"：任何会随时间长大的东西都必须有上限。
LOG_MAX_LINES = 400        # 日志文本框最多保留多少行（超出丢最旧的）
LOG_TRIM_AT = 600          # 超过这个行数就先裁一次，不必每来一行都裁
HOUSEKEEP_SEC = 30         # 巡检周期（秒）
PROC_TTL_SEC = 600         # 处理结果闲置多久后自动释放（秒）
SRC_TTL_SEC = 1800         # 载入的素材闲置多久后自动卸载（秒）——比结果长得多
CACHE_WARN_MB = 200.0      # 缓存占用超过这个数就给黄灯
CACHE_AUTO_MB = 150.0      # 缓存积累超过这个数就自动清理"较早"的（不等你闲置）
CLIP_DECAY = 2             # 削波计数每秒衰减几个，"很久以前的那次削波"会被自动忘掉
CACHE_LOG_EVERY = 20       # 每多少次巡检报告一次（20 × 30 s = 10 分钟）

BRIDGE_POLL_SEC = 2.0      # 声卡桥状态刷新周期（秒）


# ==================================================================== 声卡桥
# 上位机自己不往串口发数据 —— 那是 tool/fpga_audio_bridge.py 的活。
# 界面上要能一眼看出桥在不在跑，并能一键拉起来，否则"选了 CABLE Input
# 却没声"是最难自查的坑（链路其实没断，只是没人把虚拟线接去 FPGA）。

def _app_dir():
    """源码运行时是 host/，打包后是 dist/ACX720VoiceChanger/。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _project_root():
    """往上找包含 tool/fpga_audio_bridge.py 的那一层。"""
    d = _app_dir()
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "tool", "fpga_audio_bridge.py")):
            return d
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return None


def _bridge_ch340_port():
    """机器上唯一的 CH340 串口号；没有/多于一个返回 None。"""
    try:
        from serial.tools import list_ports
    except Exception:
        return None
    hits = []
    for p in list_ports.comports():
        blob = "%s %s %s" % (p.device, p.description or "", p.hwid or "")
        if "1A86" in blob.upper() or "CH340" in blob.upper():
            hits.append(p.device)
    return hits[0] if len(hits) == 1 else None


def _find_bridge_python():
    """找一个装了 pyserial/numpy/sounddevice 的解释器。

    打包成 exe 之后 sys.executable 是 exe 本身，跑不了 .py，所以必须另找
    一个真解释器。逐个试到能 import 三个库为止（试错很便宜，30 秒封顶）。
    """
    cands = []
    env = os.environ.get("ACX720_PYTHON")
    if env:
        cands.append(env)
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    home = os.path.expanduser("~")
    # 本机打包用的 dsh 运行时解释器（装齐了三个库）
    cands += sorted(glob.glob(os.path.join(
        home, ".dsh", "dsh-runtimes", "*", "dependencies",
        "python", "python.exe")), reverse=True)
    cands += sorted(glob.glob(os.path.join(
        home, "AppData", "Local", "Programs", "Python", "Python3*",
        "python.exe")), reverse=True)
    which = shutil.which("python")
    if which:
        cands.append(which)

    seen, probe = set(), "import serial, sounddevice, numpy"
    for py in cands:
        if not py or py in seen or not os.path.isfile(py):
            continue
        seen.add(py)
        try:
            r = subprocess.run([py, "-c", probe], timeout=30,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


# ==================================================================== 实时引擎

class RealtimeEngine(object):
    """麦克风 -> 效果链 -> 扬声器。

    输入输出故意开成两条独立的流而不是 duplex：Windows 上不同设备往往
    挂在不同的 host API（麦克风常用 MME，耳机常用 WASAPI），强行 duplex
    会直接报 PortAudio 错。两条流之间用一个环形队列缓冲，
    代价是多一点延迟，换来极高的兼容性。

    处理放在输出回调里做，输入回调只负责搬运，这样录音端不会因为
    偶尔的计算抖动而丢样本。

    ★ 双声道：输入最多取两路，一律规整成 (n, 2) 后再交给效果链；
      每条效果链的延迟线、相位、回声历史完全独立，观测量（峰值、削波、
      频谱）也按声道分开统计，绝不把 L/R 混成单声道去算。

    ★ 效果链用 dsp.EngineChain（模块 × 引擎统一入口），所以
      「变调/分离」×「FPGA 直算/FPGA 内部模型/计算机本地模型」四种组合
      在这里是同一条代码路径，没有哪一种需要特殊照顾。各自的实际延迟
      由 EngineChain.latency_ms() 如实报到界面上（见 _rt_toggle）。
    """

    def __init__(self):
        self.running = False
        self.q = queue.Queue(maxsize=128)
        self.chain = dsp.EngineChain()
        self.lock = threading.Lock()
        self.underrun = 0
        self.overflow = 0
        self.level = 0.0
        self.error = None
        # 频谱与状态校验用的观测量（音频线程写、GUI 线程读，单写单读不锁）
        # 每个声道各一套：索引 0 = 左，1 = 右
        self.ring_in = [specmod.RingBuf(4096), specmod.RingBuf(4096)]
        self.ring_out = [specmod.RingBuf(4096), specmod.RingBuf(4096)]
        self.peak_in = [0.0, 0.0]
        self.peak_out = [0.0, 0.0]
        # clip_* 是**近期**削波计数：按有效样本数每秒衰减一次，所以很久
        # 以前的那次过载不会把灯永久钉在红色上（长时间挂机最怕这个）。
        # clip_*_total 才是累计值，只用于日志。
        self.clip_in = [0, 0]
        self.clip_out = [0, 0]
        self.clip_in_total = [0, 0]
        self.clip_out_total = [0, 0]
        self._decay_acc = 0
        self._in = None
        self._out = None
        self._rest = None            # 输入队列里多出来的样本
        self._orest = None           # 效果链多吐出来的样本（分离模式会用）
        self.channels = 2
        self.in_channels = 2
        self.sr_in = 48000
        self.sr_out = 48000
        self.get_cfg = None
        # ------------------------------------------------------------
        # 效果链跑在独立 worker 线程里，音频回调只做搬运。
        # 为什么必须拆开：chain.process_stream 在「分离 × 本地模型」下
        # 内部是 demucs 推理，单块几百 ms，而 1024 点 @48k 的回调预算只有
        # 21 ms —— 放在回调里必然持续 underrun/爆音，还会把 Tk 变量读写
        # 和 chain 状态搅在一起（tkinter 不是线程安全的）。
        # 拆开后：回调只 put 输入块 / get 输出块，延迟增加一块（≈21 ms），
        # 换来"重活不再阻塞声卡回调"这条硬保证。
        self.q_in = queue.Queue(maxsize=32)     # 回调 -> worker
        self.q_out = queue.Queue(maxsize=16)    # worker -> 回调
        self._worker = None
        self._worker_stop = threading.Event()
        self.blocksize = 0
        # 防啸叫：只在 worker 线程里创建/使用（单线程独占，不需要锁）。
        #   开关一关就丢掉状态，下次打开从零开始学啸叫频点 —— 否则会拿着
        #   上一次的旧陷波去削一段完全不同的素材。
        self.howl = None

    # ------------------------------------------------------------------
    def _decay_clip(self, frames):
        """按音频时长把"近期削波计数"往下削。只在音频线程里调用（单写者）。"""
        self._decay_acc += int(frames)
        sec = int(self.sr_out) if self.sr_out else 48000
        while self._decay_acc >= sec:
            self._decay_acc -= sec
            for arr in (self.clip_in, self.clip_out):
                for i in (0, 1):
                    arr[i] = arr[i] - CLIP_DECAY if arr[i] > CLIP_DECAY else 0

    # ------------------------------------------------------------------
    def _in_cb(self, indata, frames, tinfo, status):
        if status:
            self.overflow += 1
        if not self.running:
            return
        block = np.array(indata, dtype=np.float32, copy=True)
        if self.sr_in != self.sr_out:
            block = audio_io.resample(block, self.sr_in, self.sr_out)
        # 单声道源复制成两路（两条链各自独立运算），多声道只取前两路
        block = audio_io.to_stereo(block)
        for ci in (0, 1):
            x = block[:, ci]
            self.ring_in[ci].push(x)
            pk = float(np.max(np.abs(x))) if x.size else 0.0
            if pk > self.peak_in[ci]:
                self.peak_in[ci] = pk
            else:
                self.peak_in[ci] *= 0.85
            if pk >= 0.995:
                self.clip_in[ci] += 1
                self.clip_in_total[ci] += 1
        try:
            self.q.put_nowait(block)
        except queue.Full:
            self.overflow += 1

    # ------------------------------------------------------------------
    def _out_cb(self, outdata, frames, ctime, status):
        if status:
            self.underrun += 1
        need = frames
        parts = []
        got = 0
        if self._rest is not None and self._rest.shape[0]:
            parts.append(self._rest)
            got += self._rest.shape[0]

        while got < need:
            try:
                b = self.q.get_nowait()
            except queue.Empty:
                self.underrun += 1
                break
            parts.append(b)
            got += b.shape[0]

        if got == 0:
            outdata[:] = 0.0
            self.level *= 0.9
            return

        data = parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)
        if data.ndim == 1:
            data = np.repeat(data.reshape(-1, 1), 2, axis=1)
        if data.shape[0] > need:
            self._rest = data[need:].copy()
            data = data[:need]
        else:
            self._rest = None

        # 效果链交给 worker 线程（见 __init__ 里的说明）：这里只搬运，
        # 回调内不做推理、不碰 tkinter，保证 21 ms 的回调预算不被吃掉。
        self._submit(data)
        out = self._take(need)

        if out.ndim == 1:
            out = np.repeat(out.reshape(-1, 1), 2, axis=1)
        if self._orest is not None and self._orest.shape[0]:
            out = (np.concatenate([self._orest, out], axis=0)
                   if out.shape[0] else self._orest)
        # 防御性上限：正常情况下 _orest 只该有算法延迟那么多（约 NFFT 点）。
        # 万一状态出问题让它一直涨，这里兜住，不让它把内存吃光。
        if out.shape[0] > 64 * need:
            self._orest = None
            out = out[:need]
        elif out.shape[0] > need:
            self._orest = out[need:].copy()
            out = out[:need]
        else:
            self._orest = None
        if out.shape[0] < need:
            out = np.concatenate(
                [out, np.zeros((need - out.shape[0], 2), dtype=np.float32)], axis=0)
        outdata[:] = out

        rms = float(np.sqrt(np.mean(out.astype(np.float64) ** 2))) if out.size else 0.0
        self.level = max(rms, self.level * 0.85)
        for ci in (0, 1):
            x = out[:, ci]
            self.ring_out[ci].push(x)
            pk = float(np.max(np.abs(x))) if x.size else 0.0
            if pk > self.peak_out[ci]:
                self.peak_out[ci] = pk
            else:
                self.peak_out[ci] *= 0.85
            if pk >= 0.995:
                self.clip_out[ci] += 1
                self.clip_out_total[ci] += 1
        self._decay_clip(need)

    # ------------------------------------------------------------------
    def _submit(self, block):
        """把一块输入交给 worker。回调线程专用。"""
        try:
            self.q_in.put_nowait(block)
        except queue.Full:
            # worker 跟不上（典型是 demucs 单块耗时超过一块音频的时长）：
            # 丢最旧、塞最新。宁可跳掉一小段，也不要让延迟无限累积到
            # 几十秒之后——那样用户会以为程序死了。
            self.overflow += 1
            try:
                self.q_in.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q_in.put_nowait(block)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    def _take(self, need):
        """取回一块已处理好的输出。回调线程专用。"""
        try:
            return self.q_out.get_nowait()
        except queue.Empty:
            # 还没算出来就先静音，并把 underrun 记上（界面上能看到）。
            # 为什么不直通原始输入：直通会让"分离没生效"和"处理慢"两种
            # 现象长得一样，排查时无从下手；静音至少是诚实的。
            self.underrun += 1
            return np.zeros((need, 2), dtype=np.float32)

    # ------------------------------------------------------------------
    def _process_block(self, block):
        """真正跑效果链的地方。只由 worker 线程调用。"""
        try:
            cfg = self.get_cfg() if self.get_cfg else {}
            ig = float(cfg.get("ingain", 1.0))
            if abs(ig - 1.0) > 1e-9:
                block = np.clip(block * ig, -1.0, 1.0)
            # 模块 × 引擎一次性交给 EngineChain，它内部按组合选实现：
            #   pitch/fpga_direct -> VoiceChain（int16，与 RTL 对齐）
            #   pitch/fpga_model  -> hq_stream.StreamPitchShifter
            #   split/fpga_direct -> VocalSplitter
            #   split/local_model -> local_stream.StreamSeparator（demucs 流式）
            ai = cfg.get("ai") or {}
            # self.lock 保护 chain：主线程点「复位效果链」时不会和这里
            # 撞车（原来这个锁建了却没用，链状态会被撕开）。
            with self.lock:
                self.chain.configure(
                    module=cfg.get("module", dsp.MODULE_PITCH),
                    engine=cfg.get("engine", dsp.ENGINE_FPGA_DIRECT),
                    source=dsp.SRC_REALTIME,
                    semi=cfg.get("semi_exact", 0.0),
                    formant=cfg.get("formant", 1.0),
                    split=cfg.get("split"),
                    vocal_gain=cfg.get("vocal_gain", ai.get("vocal_gain")),
                    acc_gain=cfg.get("acc_gain", ai.get("acc_gain")),
                    ai_quality=ai.get("quality"),
                    ai_device=ai.get("device"),
                    dphase=cfg.get("dphase", 0),
                    ring_freq=cfg.get("ring_freq", 1),
                    echo_taps=cfg.get("echo_taps", 8),
                    echo_gain=cfg.get("echo_gain", 2),
                    gain=cfg.get("gain", 1.0),
                )
                pcm_i = audio_io.to_int16(block)
                # 流式接口：process_stream 保证每次返回与输入严格等长
                # （内部有预热 + 输出 FIFO 配平），播放端不需要知道算法细节
                pcm_o = self.chain.process_stream(pcm_i)
            out = audio_io.to_float(pcm_o)
            # 防啸叫：串在**效果链之后**。理由是啸叫由"变声后的声音经喇叭
            # 再被麦克风收回"形成，要压的就是已经变过声的那一路。
            if cfg.get("anti_howl"):
                if self.howl is None:
                    import anti_howl
                    self.howl = anti_howl.HowlSuppressor(self.sr_in or self.sr_out)
                out = self.howl.process(out)
            elif self.howl is not None:
                self.howl = None
            return out
        except Exception as e:
            self.error = repr(e)
            return block

    # ------------------------------------------------------------------
    def _worker_loop(self):
        # 预热两块静音把 q_out 填上，输出流一起来就不会先 underrun 几块。
        for _ in range(2):
            if self.blocksize > 0:
                out = self._process_block(
                    np.zeros((self.blocksize, 2), dtype=np.float32))
                if out.ndim == 1:
                    out = np.repeat(out.reshape(-1, 1), 2, axis=1)
                self.q_out.put(out)
        while not self._worker_stop.is_set():
            try:
                block = self.q_in.get(timeout=0.2)
            except queue.Empty:
                continue
            out = self._process_block(block)
            if out.ndim == 1:
                out = np.repeat(out.reshape(-1, 1), 2, axis=1)
            try:
                self.q_out.put_nowait(out)
            except queue.Full:
                try:
                    self.q_out.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.q_out.put_nowait(out)
                except queue.Full:
                    pass

    # ------------------------------------------------------------------
    def reset_chain(self):
        """复位效果链状态。主线程调用，与 worker 互斥。"""
        with self.lock:
            self.chain.reset()
        # 复位后管道里还压着用旧状态算出来的块，一起清掉，
        # 否则会先听到一小段"复位前"的声音。
        for q in (self.q_in, self.q_out):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    # ------------------------------------------------------------------
    @staticmethod
    def _input_channels(dev):
        """尽量取两路输入；设备只有一路就取一路，后面再复制成两路。"""
        try:
            cap = int(sd.query_devices(dev).get("max_input_channels", 2) or 2)
        except Exception:
            cap = 2
        return max(1, min(2, cap))

    # ------------------------------------------------------------------
    def start(self, in_dev, out_dev, sr_in, sr_out, blocksize, channels, get_cfg):
        if not HAVE_SD:
            raise RuntimeError("sounddevice 未安装")
        if self.running:
            return
        self.get_cfg = get_cfg
        self.channels = int(channels)
        self.in_channels = self._input_channels(in_dev)
        self.sr_in = int(sr_in)
        self.sr_out = int(sr_out)
        self.underrun = 0
        self.overflow = 0
        self.error = None
        self._rest = None
        self._orest = None
        for rb in self.ring_in + self.ring_out:
            rb.reset()
        self.peak_in = [0.0, 0.0]
        self.peak_out = [0.0, 0.0]
        self.clip_in = [0, 0]
        self.clip_out = [0, 0]
        self.clip_in_total = [0, 0]
        self.clip_out_total = [0, 0]
        self._decay_acc = 0
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        for q in (self.q_in, self.q_out):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self.blocksize = int(blocksize)
        self.chain.reset()
        self.running = True
        self._worker_stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        try:
            self._in = sd.InputStream(samplerate=self.sr_in, device=in_dev,
                                      channels=self.in_channels,
                                      blocksize=int(blocksize),
                                      dtype="float32", callback=self._in_cb,
                                      latency="low")
            self._in.start()
            # 先让输入攒几块，避免输出刚起来就 underrun
            time.sleep(0.15)
            self._out = sd.OutputStream(samplerate=self.sr_out, device=out_dev,
                                        channels=self.channels, blocksize=int(blocksize),
                                        dtype="float32", callback=self._out_cb,
                                        latency="low")
            self._out.start()
        except Exception:
            self.running = False
            self.stop()
            raise

    # ------------------------------------------------------------------
    def stop(self):
        self.running = False
        for s in (self._out, self._in):
            try:
                if s is not None:
                    s.stop()
                    s.close()
            except Exception:
                pass
        self._out = None
        self._in = None
        # 先停 worker 再清队列：否则 worker 可能正卡在 demucs 推理里，
        # 主线程清完队列它又往回塞一块。
        self._worker_stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        for q in (self.q, self.q_in, self.q_out):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    # ------------------------------------------------------------------
    def load(self):
        return int(self.q.qsize()) if self.running else 0


# ==================================================================== GUI

class App(tk.Tk):

    def report_callback_exception(self, exc, val, tb):
        """Tk 回调里没接住的异常：先落盘（带 traceback），再明确告诉用户。

        ★ 默认行为是往 stderr 打一行——窗口版 EXE 里 stderr 是空的，
          于是现象变成"点了没反应"或"只有个奇怪的框"，什么线索都没有。
          默认值不能改，所以在这里覆盖。
        """
        import traceback
        txt = "".join(traceback.format_exception(exc, val, tb))
        try:
            file_log("TK-CALLBACK-EXCEPTION\n" + txt)
        except Exception:
            pass
        try:
            messagebox.showerror(
                "程序内部错误",
                "%s: %s\n\n完整堆栈已写入日志：\n%s" % (exc.__name__, val, log_path()))
        except Exception:
            pass

    def __init__(self):
        tk.Tk.__init__(self)
        # 主题必须第一个装：后面每个控件的样式都从 ttk.Style 里取，
        # 装晚了先建出来的控件还是默认浅色。
        theme.apply(self)
        self.title("ACX720 变声器 上位机  (XC7A35T)")
        # 先按内容需要的尺寸建窗口，再按屏幕收夹。
        # 为什么不写死 1240x900：内容实际要 1123 px 高，写死 900 会把
        # Notebook 压成 115 px，「开始处理」按钮直接看不见（实测过）。
        # 左栏参数面板已做成可滚动，所以矮屏幕上是左栏出滚动条，
        # 不会把窗口撑到屏幕外。
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = 1240 if sw >= 1500 else max(1080, int(sw * 0.92))
        self.geometry("%dx%d" % (w, 900))
        self.minsize(1020, 700)

        self.rt = RealtimeEngine()
        # 参数快照：主线程写、音频线程读。音频线程绝不去 .get() Tk 变量，
        # 也不往回 .set()——tkinter 不是线程安全的，跨线程碰 Tcl 解释器会
        # 随机崩在 "main thread is not in main loop" 上。
        self._main_thread = threading.current_thread()
        self._cfg_lock = threading.Lock()
        self._cfg_snap = {}
        # 文件处理线程的中止标志（关窗口时用，见 _on_close）
        self._abort = threading.Event()
        self._proc_thread = None
        # proc_data / proc_sr / _proc_at 由工作线程写、巡检线程读，
        # 必须整组原子赋值，否则巡检可能看到"新数据 + 旧时间戳"而把
        # 刚算完的结果当成闲置缓存释放掉。
        self._proc_lock = threading.Lock()
        # 内嵌播放器：文件页里播放"处理结果 / 原声"，可边放边开关左右声道
        self.player = playermod.Player(log=self._player_log)
        self.file_data = None
        self.file_sr = 48000
        self.file_path = ""
        self.proc_data = None
        self.proc_sr = 48000
        self._proc_split_info = None
        self._ai_last_info = None     # 上一次本地 AI 处理的链路信息
        self._ai_probed = False       # 本地 AI 环境自检是否已完成（后台线程填）
        self.playing = False
        self._processing = False
        self._last_advice = ""
        # 缓冲灯的滑动窗口样本 (time, overflow, underrun)——按"近期增量"
        # 判灯，累计值只进日志；否则历史抖动会让灯永远红着
        self._buf_hist = []

        # ---- 长时间运行的缓存管理状态 ----
        self._started_at = time.time()
        self._proc_at = 0.0          # 处理结果最后一次被使用的时间
        self._file_at = 0.0          # 素材最后一次被使用的时间
        self._play_data = None       # 正在播放的数据（播放期间必须保活）
        self._hk_tick = 0
        self._hk_cleaned = 0         # 自动清理次数
        self._hk_last = ""           # 最近一次清理说明
        self._log_lines = 0          # 日志当前行数（用于裁剪判断）
        self._progress_pct = -1      # 文件处理进度上报节流
        # ★ 工作线程不能直接调 self.after()：Tk 不是线程安全的，demucs 每秒
        #   几十次进度回调会把 Tcl 锁卡死，表现就是进度停在 100%、窗口无响应。
        #   统一塞进队列，由主线程 _poll() 取出来执行。
        self._ui_q = queue.Queue(maxsize=300)

        # ---- 声卡桥（CABLE Input → FPGA → STM32 → 音响）----
        self._mode_t = 0.0           # 上次发布 mode.json 的时间
        self._mode_txt = ""          # 上次发布的内容（一样就不重写）
        self._bridge_proc = None     # 本程序拉起来的那个；None = 不是我们启动的
        self._bridge_py = None       # 找到的解释器（缓存，避免每次都去试）
        self._bridge_t = 0.0         # 上次刷新状态的时间

        self._build_vars()
        self._build_ui()
        self._refresh_labels()
        self._fit_to_content()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_poll()
        self._start_housekeep()
        self._start_ai_probe()

    def _fit_to_content(self):
        """按两个标签页里更高的那个定窗口高度，塞不下再按屏幕收夹。

        为什么不让 tkinter 自己算：Notebook 的 reqheight 只反映当前选中
        那一页，切到另一页就可能高出 100 px。所以这里把每一页都选一遍取
        最大值，保证切页不裁剪。
        """
        try:
            self.update_idletasks()
            need = int(self.winfo_reqheight())
            cur = self.nb.index("current")
            for i in range(len(self.nb.tabs())):
                self.nb.select(i)
                self.update_idletasks()
                need = max(need, int(self.winfo_reqheight()))
            self.nb.select(cur)
            self.update_idletasks()

            sh = self.winfo_screenheight()
            w = self.winfo_width()
            h = min(need + 6, sh - 110)          # 给任务栏和窗口装饰留地方
            h = max(h, 700)
            if abs(h - self.winfo_height()) > 4:
                self.geometry("%dx%d" % (w, h))
        except Exception:
            pass                                  # 量不出来就用默认尺寸，不影响功能

    # ------------------------------------------------------------------
    def _start_ai_probe(self):
        """后台做本地 AI 的环境自检。

        probe() 第一次要 import torch，实测 3~6 秒。放在主线程里做会让
        窗口启动卡住，所以丢到后台线程，查完再把结果刷到界面上。
        """
        def work():
            try:
                txt = local_ai.summary_text()
                ok, why = local_ai.demucs_ready()
                rvc_ok, rvc_why = local_ai.rvc_status("")
            except Exception as e:
                txt, ok, why, rvc_ok, rvc_why = (
                    "本地 AI 自检失败：%s" % e, False, repr(e), False, "")
            self._post(self._ai_env_ready, txt, ok, why)

        if local_ai is None:
            self._ai_probed = True
            if hasattr(self, "lb_ai_env"):
                self.lb_ai_env.configure(text="本地 AI 模块未加载：%s" % LOCAL_AI_WHY)
            return
        t = threading.Thread(target=work, daemon=True)
        t.start()

    def _ai_env_ready(self, txt, ok, why):
        self._ai_probed = True
        if hasattr(self, "lb_ai_env"):
            self.lb_ai_env.configure(text=txt,
                                     foreground=(theme.OK if ok else theme.WARN))
        if hasattr(self, "cb_ai_demucs"):
            # 不再永久置灰：没就绪也能勾选，勾选上面的开关 / 点「准备 AI 环境」
            # 都会后台挂载本机 torch + 下载权重，准备好即可用
            self.cb_ai_demucs.configure(state="normal")
            if ok:
                self.v_ai_demucs.set(True)
            elif not self.v_ai_demucs.get():
                self.lb_ai_env.configure(
                    text=txt + "\n（点「准备 AI 环境」或勾选上面的开关即可自动准备）")
        self._log("本地 AI 环境自检：" + txt)

    def _set_ai_env(self, text, fg=theme.WARN):
        if hasattr(self, "lb_ai_env"):
            try:
                self.lb_ai_env.configure(text=text, foreground=fg)
            except Exception:
                pass

    def _on_ai_demucs_toggle(self):
        """勾选「启用 AI 人声分离」即触发后台准备（挂载 torch + 下载权重）。"""
        if self.v_ai_demucs.get():
            self._prepare_ai()
        else:
            self._log("已关闭 AI 人声分离：本次按纯 DSP / 直通处理")

    def _prepare_ai(self):
        """后台准备 AI 运行环境：挂载本机 torch + 下载 htdemucs 权重。

        轻量版 EXE 不含 torch，但本机装的 torch 与冻结解释器同 ABI（都是
        CPython 3.14），所以把它挂进 sys.path 即可用，不必把 4 GB 打进包。
        无本机 torch 时给出明确引导（用 AI 完整版 / 先装 torch）。
        """
        if local_ai is None:
            self._set_ai_env("本地 AI 模块未加载：%s" % LOCAL_AI_WHY, theme.ERR)
            return
        if getattr(self, "_ai_preparing", False):
            return
        self._ai_preparing = True
        if hasattr(self, "btn_ai_prepare"):
            try:
                self.btn_ai_prepare.configure(state="disabled")
            except Exception:
                pass

        def work():
            try:
                self._post(self._set_ai_env,
                           "正在准备 AI 运行环境（挂载本机 torch / 下载模型权重）…",
                           theme.WARN)
                ok, note = local_ai.ensure_local_runtime()
                if not ok:
                    self._post(self._set_ai_env,
                               "本机未检测到 torch / demucs（%s）。\n"
                               "请用「运行上位机_AI完整版.bat」，或先\n"
                               "  pip install demucs torch（cu128）\n后重开本程序。" % note,
                               theme.ERR)
                    return
                local_ai.ensure_model_endpoint()
                if not local_ai._hf_weights_cached():
                    self._post(self._set_ai_env,
                               "正在下载 htdemucs 模型权重（约 150 MB，首次较慢）…",
                               theme.WARN)
                local_ai.ensure_model_downloaded(
                    progress=lambda s: self._post(self._set_ai_env, s, theme.WARN))
                txt = local_ai.summary_text()
                self._post(self._set_ai_env, txt, theme.OK)
                self._post(self._refresh_labels)
            except Exception as e:
                self._post(self._set_ai_env,
                           "AI 准备失败：%s" % e, theme.ERR)
            finally:
                self._ai_preparing = False
                if hasattr(self, "btn_ai_prepare"):
                    try:
                        self.btn_ai_prepare.configure(state="normal")
                    except Exception:
                        pass

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    #  状态变量
    # ------------------------------------------------------------------
    def _build_vars(self):
        # 两级结构：模块 -> 引擎。v_mode 保留给"板对照/旧测试"用，
        # 由 module+engine 反推出来（见 _sync_mode_from_engine）。
        self.v_module = tk.StringVar(value=dsp.MODULE_PITCH)
        self.v_engine = tk.StringVar(value=dsp.ENGINE_FPGA_DIRECT)
        self.v_source = tk.StringVar(value=dsp.SRC_REALTIME)
        self.v_mode = tk.IntVar(value=dsp.MODE_PITCH)
        self.v_semi = tk.DoubleVar(value=0.0)
        self.v_formant = tk.DoubleVar(value=100.0)   # 共振峰保持 %
        self.v_ring = tk.IntVar(value=1)
        self.v_taps = tk.IntVar(value=8)
        self.v_egain = tk.IntVar(value=2)
        self.v_gain = tk.DoubleVar(value=1.0)
        self.v_ingain = tk.DoubleVar(value=12.0)   # 默认 +12 dB，阵列麦克风普遍偏轻

        # ---- 分离（MODE_SPLIT）专用：左=人声 / 右=伴奏 ----
        self.v_sp_depth = tk.DoubleVar(value=100.0)   # 分离深度 %
        self.v_sp_thr = tk.IntVar(value=0)            # 兼容旧配置，界面已隐藏
        self.v_sp_swap = tk.BooleanVar(value=False)   # 人声去右 / 伴奏去左
        self.v_sp_bb = tk.DoubleVar(value=20.0)       # 非谐波中置成分保留 %（实测 20%）
        self.v_sp_harm = tk.BooleanVar(value=True)    # 谐波梳状掩蔽
        self.v_sp_vgain = tk.DoubleVar(value=100.0)   # 人声音量 %
        self.v_sp_again = tk.DoubleVar(value=100.0)   # 伴奏音量 %
        self.v_sp_bass = tk.DoubleVar(value=120.0)    # 伴奏低频保护 Hz

        # ---- 防啸叫（实时链路与文件处理共用同一个开关）----
        #   ★ 这个变量必须在**两个页签建好之前**就存在：实时页先建、
        #     文件页后建，如果只在文件页里创建，实时页就引用不到。
        self.v_anti_howl = tk.BooleanVar(value=True)

        # ---- 本地 AI 处理（MODE_AI_LOCAL）----
        self.v_ai_demucs = tk.BooleanVar(value=True)   # 用 demucs 做人声分离
        self.v_ai_q = tk.IntVar(value=2)               # QUALITY_NAMES 下标（2=高质/htdemucs_ft）
        self.v_ai_dev = tk.IntVar(value=0)             # 0 自动 / 1 CUDA / 2 CPU
        self.v_ai_voice = tk.IntVar(value=1)           # VOICE_NAMES 下标
        self.v_ai_semi = tk.DoubleVar(value=4.0)       # 人声变调半音
        self.v_ai_formant = tk.DoubleVar(value=100.0)  # 共振峰保持 %
        self.v_ai_vgain = tk.DoubleVar(value=100.0)    # 人声音量 %
        self.v_ai_again = tk.DoubleVar(value=100.0)    # 伴奏音量 %
        self.v_ai_rvc_dir = tk.StringVar(value="")

        # 文件模式下的变调算法由「引擎」承担，不再有单独勾选框：
        #   FPGA 直算       -> 时域延迟线（与板子一致）
        #   FPGA 内部小模型 -> 相位声码器 + 共振峰保持（质量最好）

        self.v_indev = tk.StringVar(value="")
        self.v_outdev = tk.StringVar(value="")
        self.v_sr = tk.IntVar(value=48000)
        self.v_block = tk.IntVar(value=1024)
        self.v_spec_mode = tk.StringVar(value="同屏四路")   # 四路波形 / 频谱的显示模式
        self.v_view = tk.StringVar(value="四路波形")        # 右上图：四路波形 / 频谱对照

        self.v_infile = tk.StringVar(value="")
        self.v_outfile = tk.StringVar(value="")

        for v in (self.v_module, self.v_engine, self.v_source,
                  self.v_semi, self.v_formant, self.v_ring, self.v_taps,
                  self.v_egain, self.v_gain, self.v_ingain,
                  self.v_sp_depth, self.v_sp_thr, self.v_sp_swap,
                  self.v_sp_bb, self.v_sp_harm, self.v_sp_vgain, self.v_sp_again,
                  self.v_sp_bass,
                  self.v_ai_demucs, self.v_ai_q, self.v_ai_dev,
                  self.v_ai_voice, self.v_ai_semi, self.v_ai_formant,
                  self.v_ai_vgain, self.v_ai_again):
            v.trace_add("write", lambda *a: self._refresh_labels())

    # ------------------------------------------------------------------
    def _build_ui(self):
        # 根布局：顶部标题栏 / 左参数 / 右功能 / 底部日志
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=0)    # 标题栏
        self.rowconfigure(1, weight=1)    # 主区
        self.rowconfigure(2, weight=0)    # 日志

        self._build_header()

        # 左栏固定宽度 + 可滚动：参数面板的高度随组合变化（最高那组 350 px），
        # 窗口矮的时候靠滚动条兜底，不裁卡片。
        left_host = ttk.Frame(self)
        left_host.grid(row=1, column=0, sticky="ns", padx=(12, 6))
        left_host.rowconfigure(0, weight=1)
        left_host.columnconfigure(0, weight=1)
        _sc, left = theme.scrollable(left_host, width=396)
        _sc.grid(row=0, column=0, sticky="nsew")

        right = ttk.Frame(self, padding=(6, 10, 12, 10))
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(0, weight=0)    # 频谱：固定高度
        right.rowconfigure(1, weight=1)    # 工作模式：吃掉剩余空间
        right.rowconfigure(2, weight=0)    # 状态灯板
        right.columnconfigure(0, weight=1)

        # 日志框必须**最先**建：_build_params 结尾会调 _on_module_change ->
        # _on_engine_change -> _log，那时 self.log 必须已经存在。
        # （曾经把 _build_log 放最后，GUI 构建直接 AttributeError。）
        self._build_log()
        self._build_params(left)
        self._build_spectrum(right)
        self._build_tabs(right)
        self._build_status(right)

    # ------------------------------------------------------------------
    def _build_header(self):
        """顶部标题栏：左边应用名，右边两个实时徽章（当前组合 / 板上档位）。

        做成 Canvas 药丸而不是 Label：圆角 + 等宽数字看起来像仪器面板，
        而且改字不用重建控件（set_pill_text 直接改 item 的 text 选项）。
        """
        h = ttk.Frame(self, style="Bar.TFrame", padding=(14, 8))
        h.grid(row=0, column=0, columnspan=2, sticky="ew")
        h.columnconfigure(0, weight=1)
        h.columnconfigure(1, weight=0)
        h.columnconfigure(2, weight=0)

        ttk.Label(h, text="ACX720 变声器", style="HeaderTitle.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(h, text="XC7A35T  ·  模块 × 引擎 × 输入  ·  双声道独立运算",
                  style="HeaderSub.TLabel").grid(row=1, column=0, sticky="w")

        self.pill_combo = theme.pill(h, "变调 × FPGA 直算")
        self.pill_combo.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 8))
        self.pill_sw = theme.pill(h, "sw = 00000000",
                                  fg=theme.OK, font=theme.FONT_MONO)
        self.pill_sw.grid(row=0, column=2, rowspan=2, sticky="e")

    def _refresh_pills(self):
        """把当前组合与板上档位刷到标题栏的两个徽章上。"""
        mod = self.v_module.get()
        eng = self.v_engine.get()
        # ★ ENGINE_NAMES_SHORT 是按 (模块, 引擎) 二元组索引的 —— 同一个引擎键
        #   在不同模块下名字不一样（fpga_direct 在变调下叫"直算"、在分离下叫
        #   "内部模型"）。早期版本只传了 eng，查不到就回落成原始键，
        #   徽章上于是出现 "变调 × fpga_direct" 这种内部名。
        name = dsp.MODULE_NAMES_SHORT.get(mod, mod) + " × " + \
            dsp.ENGINE_NAMES_SHORT.get((mod, eng), eng)
        theme.set_pill_text(self.pill_combo, name)
        byte = self._sw_byte()
        if byte is None:
            theme.set_pill_text(self.pill_sw, "sw = ----（板上无此组合）",
                                fg=theme.FG_FAINT)
        else:
            theme.set_pill_text(self.pill_sw, "sw = %08X" % byte, fg=theme.OK)

    def _sw_byte(self):
        """当前参数对应的板上拨码字节；这个组合板上没有就返回 None。

        抽出这个函数是因为标题栏徽章和参数面板里的 lb_sw 都要算它，
        以前只在 _refresh_labels 里写了一遍，徽章想用得抄第二遍 ——
        抄就会漂移。
        """
        if self.v_module.get() != dsp.MODULE_PITCH or \
                self.v_engine.get() != dsp.ENGINE_FPGA_DIRECT:
            return None
        if self.v_mode.get() not in dsp.BOARD_MODES:
            return None
        mode = self.v_mode.get()
        mode_sw = dsp.MODE_PITCH if mode == dsp.MODE_PITCH_ECHO else mode
        v = dsp.dphase_to_switch(dsp.semitone_to_dphase(self.v_semi.get()))
        if mode_sw == dsp.MODE_RING:
            low6 = self.v_ring.get() & 7
        elif mode_sw == dsp.MODE_ECHO:
            low6 = ((self.v_egain.get() & 3) << 4) | (self.v_taps.get() & 15)
        elif mode_sw == dsp.MODE_PITCH:
            low6 = (v & 15) << 2
        else:
            low6 = 0
        return ((mode_sw & 3) << 6) | (low6 & 0x3F)

    # ------------------------------------------------------------------
    def _build_params(self, p):
        """参数面板：模块 -> 引擎 -> 输入，三级单选，下面只显示当前组合用得到的参数。

        原来的面板是把七种模式平铺成一列单选，每个模式再跟一个自己的
        参数框，一屏放不下、而且绝大多数参数对当前模式无效。改成两级之后
        面板长度固定，参数框按 模块×引擎 动态显隐（见 _relayout）。
        """
        ttk.Label(p, text="效果参数", style="Sub.TLabel").grid(row=0, column=0, sticky="w",
                                                              pady=(0, 2))

        # ---- 模块：只有这两个 ----
        f = theme.card(p, "模块")
        f.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        for i, (key, name) in enumerate(dsp.MODULE_NAMES):
            ttk.Radiobutton(f, text=name, value=key, variable=self.v_module,
                            command=self._on_module_change).grid(
                                row=0, column=i, sticky="w")

        # ---- 引擎：两个模块各有两个，随模块切换 ----
        f_eng = theme.card(p, "引擎（谁来算）")
        f_eng.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        f_eng.columnconfigure(0, weight=1)
        self.f_eng_pitch = ttk.Frame(f_eng)
        self.f_eng_split = ttk.Frame(f_eng)
        for i, (key, nm) in enumerate((
                (dsp.ENGINE_FPGA_DIRECT, "FPGA 直算（时域延迟线）"),
                (dsp.ENGINE_AGE, "年龄变声（任意年龄 → 任意年龄）"))):
            ttk.Radiobutton(self.f_eng_pitch, text=nm, value=key,
                            variable=self.v_engine,
                            command=self._on_engine_change).grid(
                                row=i, column=0, sticky="w")
        for i, (key, nm) in enumerate((
                (dsp.ENGINE_FPGA_DIRECT, "中置提取（纯 DSP，左人声/右伴奏）"),
                (dsp.ENGINE_LOCAL_MODEL, "计算机本地模型（demucs）"))):
            ttk.Radiobutton(self.f_eng_split, text=nm, value=key,
                            variable=self.v_engine,
                            command=self._on_engine_change).grid(
                                row=i, column=0, sticky="w")
        self.f_eng_pitch.grid(row=0, column=0, sticky="ew")
        self.f_eng_split.grid(row=0, column=0, sticky="ew")

        # ---- 输入方式（已删）：与右侧「实时变声 / 文件变声」页签重复。
        #   v_source 现在只由右侧页签驱动：见 _build_tabs 里的
        #   <<NotebookTabChanged>> -> _on_tab_changed -> _on_source_change。

        # ---- 变调参数（模块=变调时显示）----
        self.f_pitch = theme.card(p, "变调参数")
        ttk.Label(self.f_pitch, text="音高偏移").grid(row=0, column=0, sticky="w")
        ttk.Scale(self.f_pitch, from_=-12, to=12, orient="horizontal",
                  variable=self.v_semi, length=190,
                  command=lambda e: self._on_semi()).grid(row=0, column=1, sticky="ew", padx=6)
        self.lb_semi = ttk.Label(self.f_pitch, text="", foreground=theme.ACCENT)
        self.lb_semi.grid(row=0, column=2, sticky="w")
        ttk.Label(self.f_pitch, text="共振峰保持").grid(row=1, column=0, sticky="w")
        ttk.Scale(self.f_pitch, from_=0, to=100, orient="horizontal",
                  variable=self.v_formant, length=190).grid(row=1, column=1, sticky="ew", padx=6)
        self.lb_formant = ttk.Label(self.f_pitch, text="", foreground=theme.ACCENT)
        self.lb_formant.grid(row=1, column=2, sticky="w")
        ttk.Label(self.f_pitch, justify="left", foreground=theme.FG_DIM, font=theme.FONT_SMALL,
                  wraplength=250,
                  text="「FPGA 直算」把整条频谱按同一个比例缩放，共振峰跟着跑，\n"
                       "听感上声道变短（小孩声）。「FPGA 内部小模型」只搬基频，\n"
                       "用倒谱包络把共振峰按回原位 —— 实测包络相关 0.94 vs 0.34。").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ---- 年龄变声参数（模块=变调 且 引擎=年龄变声 时显示）----
        #   换算全部在 host/age_model.py（Python 侧可测可打印），FPGA 只收
        #   4 个定点数；这里负责选年龄、算参数、打包下发。
        self.v_age_from = tk.StringVar(value="30")
        self.v_age_to = tk.StringVar(value="70")
        self.v_age_sex = tk.StringVar(value="男")
        self.v_age_port = tk.StringVar(value="COM5")
        self.f_age = theme.card(p, "年龄变声（任意年龄 → 任意年龄）")
        r = 0
        ttk.Label(self.f_age, text="源年龄").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(self.f_age, from_=4, to=90, width=5,
                    textvariable=self.v_age_from).grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(self.f_age, text="岁").grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.f_age, text="目标年龄").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(self.f_age, from_=4, to=90, width=5,
                    textvariable=self.v_age_to).grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(self.f_age, text="岁").grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.f_age, text="性别").grid(row=r, column=0, sticky="w")
        ttk.Combobox(self.f_age, textvariable=self.v_age_sex, width=5,
                     state="readonly", values=["男", "女"]).grid(
            row=r, column=1, sticky="w", padx=6)
        r += 1
        ttk.Label(self.f_age, text="串口").grid(row=r, column=0, sticky="w")
        ttk.Entry(self.f_age, textvariable=self.v_age_port, width=8).grid(
            row=r, column=1, columnspan=2, sticky="w", padx=6)
        r += 1
        ttk.Button(self.f_age, text="发送到 FPGA", style="Tool.TButton",
                   command=self._send_age).grid(row=r, column=0, columnspan=3,
                                                sticky="w", pady=(6, 0))
        r += 1
        self.lb_age = ttk.Label(self.f_age, text="", foreground=theme.ACCENT,
                                font=theme.FONT_SMALL, wraplength=250,
                                justify="left")
        self.lb_age.grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ---- 分离参数：中置提取（纯 DSP） ----
        self.f_split_fpga = theme.card(p, "分离参数（左=人声 / 右=伴奏）")
        r = 0
        ttk.Label(self.f_split_fpga, text="分离深度").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_split_fpga, from_=0, to=100, orient="horizontal",
                  variable=self.v_sp_depth, length=170).grid(row=r, column=1, padx=6)
        self.lb_sp_depth = ttk.Label(self.f_split_fpga, text="", foreground=theme.ACCENT)
        self.lb_sp_depth.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.f_split_fpga, text="声道路由").grid(row=r, column=0, sticky="w")
        self.cb_sp_route = ttk.Combobox(
            self.f_split_fpga, state="readonly", width=16,
            values=["人声→左 / 伴奏→右", "人声→右 / 伴奏→左"])
        self.cb_sp_route.current(1 if self.v_sp_swap.get() else 0)
        self.cb_sp_route.grid(row=r, column=1, columnspan=2, sticky="w",
                              padx=6, pady=3)
        self.cb_sp_route.bind("<<ComboboxSelected>>", self._on_sp_route)
        r += 1
        ttk.Label(self.f_split_fpga, text="非谐波保留").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_split_fpga, from_=0, to=100, orient="horizontal",
                  variable=self.v_sp_bb, length=170).grid(row=r, column=1, padx=6)
        self.lb_sp_bb = ttk.Label(self.f_split_fpga, text="", foreground=theme.ACCENT)
        self.lb_sp_bb.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Checkbutton(self.f_split_fpga, text="谐波梳状掩蔽（人声更干净）",
                        variable=self.v_sp_harm).grid(row=r, column=0, columnspan=3,
                                                      sticky="w")
        r += 1
        ttk.Label(self.f_split_fpga, text="伴奏低频保护").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_split_fpga, from_=0, to=300, orient="horizontal",
                  variable=self.v_sp_bass, length=170).grid(row=r, column=1, padx=6)
        self.lb_sp_bass = ttk.Label(self.f_split_fpga, text="",
                                    foreground=theme.ACCENT)
        self.lb_sp_bass.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.f_split_fpga, text="人声音量").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_split_fpga, from_=0, to=200, orient="horizontal",
                  variable=self.v_sp_vgain, length=170).grid(row=r, column=1, padx=6)
        self.lb_sp_vgain = ttk.Label(self.f_split_fpga, text="",
                                     foreground=theme.ACCENT)
        self.lb_sp_vgain.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.f_split_fpga, text="伴奏音量").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_split_fpga, from_=0, to=200, orient="horizontal",
                  variable=self.v_sp_again, length=170).grid(row=r, column=1, padx=6)
        self.lb_sp_again = ttk.Label(self.f_split_fpga, text="",
                                     foreground=theme.ACCENT)
        self.lb_sp_again.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.f_split_fpga, justify="left", foreground=theme.FG_DIM,
                  font=theme.FONT_SMALL, wraplength=250,
                  text="原理：人声通常居中，伴奏带左右差异。用 mid/side 提中置，\n"
                       "再用逐帧基频的谐波梳把贝斯/底鼓挡在人声之外。\n"
                       "输出：左声道只放人声，右声道只放伴奏。").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ---- 分离参数：计算机本地模型 ----
        self.f_split_local = theme.card(p, "分离参数（左=人声 / 右=伴奏）")
        r = 0
        self.lb_ai_env = ttk.Label(self.f_split_local, text="环境自检中…",
                                   foreground=theme.WARN, justify="left",
                                   wraplength=250, font=theme.FONT_SMALL)
        self.lb_ai_env.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        # 启用 AI 分离开关 + 准备按钮：勾选即触发后台"挂载本机 torch / 下载权重"
        self.cb_ai_demucs = ttk.Checkbutton(
            self.f_split_local, text="启用 AI 人声分离（demucs 神经网络）",
            variable=self.v_ai_demucs, command=self._on_ai_demucs_toggle)
        self.cb_ai_demucs.grid(row=r, column=0, columnspan=2, sticky="w")
        self.btn_ai_prepare = ttk.Button(
            self.f_split_local, text="准备 AI 环境", command=self._prepare_ai)
        self.btn_ai_prepare.grid(row=r, column=2, sticky="e")
        r += 1

        ttk.Label(self.f_split_local, text="分离质量").grid(row=r, column=0, sticky="w")
        self.cb_ai_q = ttk.Combobox(self.f_split_local, state="readonly", width=22,
                                    values=_AI_QNAMES)
        self.cb_ai_q.current(self.v_ai_q.get())
        self.cb_ai_q.grid(row=r, column=1, columnspan=2, sticky="w", padx=6, pady=2)
        self.cb_ai_q.bind("<<ComboboxSelected>>",
                          lambda e: self.v_ai_q.set(self.cb_ai_q.current()))
        r += 1

        ttk.Label(self.f_split_local, text="运算设备").grid(row=r, column=0, sticky="w")
        self.cb_ai_dev = ttk.Combobox(
            self.f_split_local, state="readonly", width=22,
            values=["自动（有 CUDA 就用）", "只用 CUDA", "只用 CPU（慢 5~15 倍）"])
        self.cb_ai_dev.current(self.v_ai_dev.get())
        self.cb_ai_dev.grid(row=r, column=1, columnspan=2, sticky="w", padx=6, pady=2)
        self.cb_ai_dev.bind("<<ComboboxSelected>>",
                            lambda e: self.v_ai_dev.set(self.cb_ai_dev.current()))
        r += 1

        ttk.Label(self.f_split_local, text="声道路由").grid(row=r, column=0, sticky="w")
        self.cb_ai_route = ttk.Combobox(
            self.f_split_local, state="readonly", width=22,
            values=["人声→左 / 伴奏→右", "人声→右 / 伴奏→左"])
        self.cb_ai_route.current(1 if self.v_sp_swap.get() else 0)
        self.cb_ai_route.grid(row=r, column=1, columnspan=2, sticky="w",
                              padx=6, pady=2)
        self.cb_ai_route.bind("<<ComboboxSelected>>", self._on_sp_route)
        r += 1

        ttk.Label(self.f_split_local, text="人声音量").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_split_local, from_=0, to=200, orient="horizontal",
                  variable=self.v_ai_vgain, length=150).grid(row=r, column=1, padx=6)
        self.lb_ai_vgain = ttk.Label(self.f_split_local, text="", foreground=theme.ACCENT)
        self.lb_ai_vgain.grid(row=r, column=2, sticky="w")
        r += 1

        ttk.Label(self.f_split_local, text="伴奏音量").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_split_local, from_=0, to=200, orient="horizontal",
                  variable=self.v_ai_again, length=150).grid(row=r, column=1, padx=6)
        self.lb_ai_again = ttk.Label(self.f_split_local, text="", foreground=theme.ACCENT)
        self.lb_ai_again.grid(row=r, column=2, sticky="w")
        r += 1

        self.lb_ai_lat = ttk.Label(self.f_split_local, text="", foreground=theme.WARN,
                                   font=theme.FONT_SMALL, wraplength=250, justify="left")
        self.lb_ai_lat.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(self.f_split_local, justify="left", foreground=theme.FG_DIM,
                  font=theme.FONT_SMALL, wraplength=250,
                  text="全程在本机跑：不联网、不上传任何音频。\n"
                       "实时模式下 demucs 按块流着算，延迟就是上面那个数；\n"
                       "文件模式下走整段离线，质量最好。").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---- 音量 ----
        self.f_gain = theme.card(p, "输出音量")
        ttk.Scale(self.f_gain, from_=0, to=200, orient="horizontal",
                  variable=self.v_gain, length=200).grid(row=0, column=0, sticky="ew")
        self.lb_gain = ttk.Label(self.f_gain, text="")
        self.lb_gain.grid(row=1, column=0, sticky="w")

        # ---- 板同步提示 ----
        self.f_sw = theme.card(p, "对应板上的拨码开关")
        self.lb_sw = ttk.Label(self.f_sw, text="", font=theme.FONT_MONO_B,
                               foreground=theme.OK)
        self.lb_sw.grid(row=0, column=0, sticky="w")
        ttk.Label(self.f_sw, text="（sw[7:6] 选模式，其余位含义随模式变）",
                  style="Faint.TLabel").grid(row=1, column=0, sticky="w")

        # ---- 机器人 / 回声：保留对象但不进主面板 ----
        # 用户把功能收敛成"变调 + 分离"两大模块，这两个附加效果从主界面
        # 撤下（代码和参数都还在，MODE_RING / MODE_ECHO 的测试继续有效）。
        self.f_aux = ttk.Frame(p)
        r = 0
        ttk.Label(self.f_aux, text="环形调制频率").grid(row=r, column=0, sticky="w")
        cb = ttk.Combobox(self.f_aux, values=["%d Hz" % f0 for f0 in dsp.RING_FREQ_HZ],
                          state="readonly", width=14)
        cb.current(self.v_ring.get())
        cb.grid(row=r, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self.v_ring.set(cb.current()))
        self._ring_cb = cb
        r += 1
        ttk.Label(self.f_aux, text="回声延迟").grid(row=r, column=0, sticky="w")
        ttk.Scale(self.f_aux, from_=1, to=15, orient="horizontal",
                  variable=self.v_taps, length=150).grid(row=r, column=1)
        self.lb_echo = ttk.Label(self.f_aux, text="", foreground=theme.ACCENT)
        self.lb_echo.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.f_aux, text="回声反馈").grid(row=r, column=0, sticky="w")
        cb2 = ttk.Combobox(self.f_aux, values=dsp.ECHO_GAIN_NAMES, state="readonly", width=8)
        cb2.current(self.v_egain.get())
        cb2.grid(row=r, column=1, sticky="w")
        cb2.bind("<<ComboboxSelected>>", lambda e: self.v_egain.set(cb2.current()))
        self._echo_cb = cb2

        # ---- 恢复默认 ----
        ttk.Button(p, text="恢复默认参数", command=self._reset_params).grid(
            row=99, column=0, sticky="ew", pady=(12, 0))

        # 动态显隐的参数框（顺序即显示顺序）
        self._param_frames = [self.f_pitch, self.f_age, self.f_split_fpga,
                              self.f_split_local, self.f_gain, self.f_sw]
        self._on_module_change()

    def _relayout(self):
        """把可见的参数框依次重排，隐藏的不占位。

        用 `_visible` 标记而不是 winfo_ismapped()：后者的值在一次
        update() 之前不可靠，切模块时会出现"该显示的没显示"。
        """
        row = 3      # 原来的「输入」卡片(row=3)已删，动态区从 3 开始
        for fr in self._param_frames:
            if getattr(fr, "_visible", True):
                fr.grid(row=row, column=0, sticky="ew", pady=(8, 0))
                row += 1
            else:
                fr.grid_remove()

    def _show_frames(self, *frames):
        """只显示给定的参数框。"""
        for fr in self._param_frames:
            fr._visible = fr in frames
        self._relayout()

    # ------------------------------------------------------------------
    def _mk_tk(self, name):
        v = tk.StringVar(value="")
        setattr(self, name, v)
        return v

    # ------------------------------------------------------------------
    def _build_spectrum(self, p):
        """右上：**四路波形**（输入 L/R + 输出 L/R），可切回频谱对照。

        2026-10-20 改版：原来的图是"上下两条频带，每条再劈成左右半格"。
        分离模式改成"左出人声、右出伴奏"之后，输出两路的语义完全不同，
        半格并排看不出来，所以默认改成四条独立泳道、各占满整宽。
        波形直接看包络：人声是断续的、伴奏是连续的，一眼能分清；
        要看频率内容时用右上角「视图」切回频谱。
        """
        f = theme.card(p, "四路波形（输入 / 输出 × 左 / 右）")
        f.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(0, weight=1)

        self.plot_host = ttk.Frame(f)
        self.plot_host.grid(row=0, column=0, sticky="ew")
        self.plot_host.columnconfigure(0, weight=1)
        self.scope = specmod.QuadScope(self.plot_host, height=220)
        self.spec = specmod.DualSpectrum(self.plot_host, height=220)
        self.scope.grid(row=0, column=0, sticky="ew")
        self.spec.grid(row=0, column=0, sticky="ew")
        self.spec.grid_remove()

        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        ttk.Label(bar, text="视图", style="Faint.TLabel").pack(side="left")
        cbv = ttk.Combobox(bar, textvariable=self.v_view, state="readonly",
                           width=10, values=["四路波形", "频谱对照"])
        cbv.pack(side="left", padx=(2, 12))
        cbv.bind("<<ComboboxSelected>>", lambda e: self._on_view_change())
        ttk.Label(bar, text="显示", style="Faint.TLabel").pack(side="left")
        cb = ttk.Combobox(bar, textvariable=self.v_spec_mode, state="readonly",
                          width=9, values=["同屏四路", "只看左", "只看右", "平均"])
        cb.pack(side="left", padx=(2, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._log(
            "显示改为「%s」（DSP 始终四路独立运算）" % self.v_spec_mode.get()))
        self.v_spec_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="实时刷新", variable=self.v_spec_on).pack(
            side="left")
        ttk.Button(bar, text="清空", style="Tool.TButton",
                   command=self._clear_plot).pack(
            side="left", padx=(12, 0))
        ttk.Label(f, style="Faint.TLabel", wraplength=880, justify="left",
                  text="上→下：输入 L / 输入 R / 输出 L / 输出 R（四路共用同一量程；"
                       "分离模式 = 左出人声、右出伴奏）"
                  ).grid(row=2, column=0, sticky="w", pady=(3, 0))

    def _on_view_change(self):
        if self.v_view.get() == "频谱对照":
            self.scope.grid_remove()
            self.spec.grid()
            self._log("右上图：频谱对照（上=输入 / 下=输出，每格左半 L、右半 R）")
        else:
            self.spec.grid_remove()
            self.scope.grid()
            self._log("右上图：四路波形（输入 L/R + 输出 L/R，四路共用同一量程）")

    def _clear_plot(self):
        try:
            self.spec.clear()
            self.scope.clear()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _build_status(self, p):
        """右下：状态校验指示灯。每 50 ms 全量刷新一次。"""
        f = theme.card(p, "状态校验")
        f.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        f.columnconfigure(0, weight=1)

        items = [
            ("dev_in", "输入设备", ""),
            ("dev_out", "输出设备", ""),
            ("sr", "采样率", ""),
            ("engine", "引擎", ""),
            ("sig_in", "输入信号", ""),
            ("sig_out", "输出信号", ""),
            ("clip_in_l", "输入电平 L", ""),
            ("clip_in_r", "输入电平 R", ""),
            ("clip_out_l", "输出电平 L", ""),
            ("clip_out_r", "输出电平 R", ""),
            ("buf", "缓冲", ""),
            ("dsp", "DSP 内核", ""),
            ("ai", "本地 AI", ""),
            ("file", "文件链路", ""),
            ("cache", "缓存", ""),
            ("sw", "板上档位", ""),
        ]
        self.leds = LedBoard(f, items, cols=8)
        self.leds.grid(row=0, column=0, sticky="ew")
        legend(f).grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.lb_advice = ttk.Label(f, text="", foreground=theme.WARN, font=theme.FONT_UI,
                                   wraplength=760, justify="left")
        self.lb_advice.grid(row=2, column=0, sticky="w", pady=(2, 0))

    # ------------------------------------------------------------------
    def _build_tabs(self, p):
        nb = ttk.Notebook(p)
        nb.grid(row=1, column=0, sticky="nsew")
        self.nb = nb
        t1 = ttk.Frame(nb, padding=8)
        t2 = ttk.Frame(nb, padding=8)
        nb.add(t1, text="  实时变声（麦克风）  ")
        nb.add(t2, text="  文件变声  ")
        # 左侧「输入」栏已删：页签是输入方式的唯一入口，切换时同步 v_source
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._build_rt_tab(t1)
        self._build_file_tab(t2)

    def _on_tab_changed(self, _evt=None):
        """页签手动切换 -> 同步 v_source，再走统一的 _on_source_change。

        只在值真的变了时才 set，避免重复打日志；_on_source_change 内部的
        nb.select() 选中的是同一页，不会再触发本回调，无递归。
        """
        try:
            idx = self.nb.index(self.nb.select())
        except Exception:
            return
        src = dsp.SRC_REALTIME if idx == 0 else dsp.SRC_FILE
        if src != self.v_source.get():
            self.v_source.set(src)
            self._on_source_change()

    # ------------------------------------------------------------------
    def _build_rt_tab(self, p):
        p.columnconfigure(1, weight=1)

        # 安全提示 + 防啸叫开关放同一行：
        #   提示说的是"外放会啸叫"，开关就在旁边，不用去别的页找。
        top = ttk.Frame(p)
        top.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        warn = ttk.Label(top, text="⚠  务必戴耳机！外放会被麦克风重新采回去，形成啸叫。",
                         foreground=theme.WARN, font=theme.FONT_UI_B,
                         padding=(8, 5))
        warn.pack(side="left")
        self.cb_howl_rt = ttk.Checkbutton(
            top, variable=self.v_anti_howl,
            text="防啸叫（外放时自动压住自激，只在啸叫频点动手）")
        self.cb_howl_rt.pack(side="left", padx=(14, 0))

        ins, outs = self._enum_devices()

        in_names = ["%s   [%s]" % (d[1], d[2]) for d in ins] or ["（无输入设备）"]
        out_names = ["%s   [%s]" % (d[1], d[2]) for d in outs] or ["（无输出设备）"]

        d_in, d_out = audio_io.default_devices()
        self.v_indev.set(in_names[0] if in_names else "")
        if self._in_idx and isinstance(d_in, int) and d_in in self._in_idx:
            self.v_indev.set(in_names[self._in_idx.index(d_in)])
        self.v_outdev.set(out_names[0] if out_names else "")
        if self._out_idx and isinstance(d_out, int) and d_out in self._out_idx:
            self.v_outdev.set(out_names[self._out_idx.index(d_out)])

        # 四列网格：标签 | 控件 | 标签 | 控件。
        # 原来是一列十行，光这一个页就要 477 px，右栏整体被撑到 1017 px，
        # 900 px 高的窗口里 Notebook 被压成 115 px，按钮全被挤掉。
        # 改成两两并排之后这一页降到 300 px 左右。
        p.columnconfigure(0, weight=0, minsize=86)
        p.columnconfigure(1, weight=1)
        p.columnconfigure(2, weight=0, minsize=86)
        p.columnconfigure(3, weight=1)

        ttk.Label(p, text="输入设备（麦克风）").grid(row=1, column=0, sticky="w")
        self.cb_in = ttk.Combobox(p, values=in_names, state="readonly",
                                  textvariable=self.v_indev)
        self.cb_in.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)
        if in_names:
            self.cb_in.current(0)

        ttk.Label(p, text="输出设备（扬声器/耳机）").grid(row=2, column=0, sticky="w")
        self.cb_out = ttk.Combobox(p, values=out_names, state="readonly",
                                   textvariable=self.v_outdev)
        self.cb_out.grid(row=2, column=1, columnspan=3, sticky="ew", pady=3)
        if out_names:
            self.cb_out.current(0)

        ttk.Label(p, text="采样率").grid(row=3, column=0, sticky="w")
        self.cb_sr = ttk.Combobox(p, values=[str(s) for s in SR_CHOICES],
                                  state="readonly", width=10)
        self.cb_sr.set("48000")
        self.cb_sr.grid(row=3, column=1, sticky="w", pady=3)

        ttk.Label(p, text="缓冲块大小").grid(row=3, column=2, sticky="w")
        self.cb_blk = ttk.Combobox(p, values=[str(b) for b in BLOCK_CHOICES],
                                   state="readonly", width=10)
        self.cb_blk.set("1024")
        self.cb_blk.grid(row=3, column=3, sticky="w", pady=3)

        # 阵列麦克风的原始电平往往很小（实测本机 RMS 只有 0.0005），
        # 必须给一个前级放大，否则变出来几乎听不见。
        ttk.Label(p, text="输入增益").grid(row=4, column=0, sticky="w")
        sc_in = ttk.Scale(p, from_=0.0, to=30.0, orient="horizontal",
                          variable=self.v_ingain, length=200)
        sc_in.grid(row=4, column=1, sticky="ew", padx=6, pady=3)
        self.lb_ingain = ttk.Label(p, text="", foreground=theme.ACCENT)
        self.lb_ingain.grid(row=4, column=2, columnspan=2, sticky="w")
        ttk.Label(p, style="Faint.TLabel",
                  text="块越小延迟越低，太小时会卡顿爆音"
                  ).grid(row=4, column=3, sticky="e")

        btns = ttk.Frame(p)
        btns.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 4))
        self.btn_rt = ttk.Button(btns, text="启动实时变声", style="Accent.TButton",
                                 command=self._rt_toggle)
        self.btn_rt.pack(side="left")
        ttk.Button(btns, text="刷新设备列表", style="Tool.TButton",
                   command=self._refresh_devices).pack(side="left", padx=8)
        ttk.Button(btns, text="清空效果缓存", style="Tool.TButton",
                   command=self._rt_reset).pack(side="left")

        # ---- 声卡桥：CABLE Input → FPGA → STM32 → 音响 这一段不在本进程里 ----
        # 选了 CABLE Input 却没开桥 = 静音，而且从界面上完全看不出来，
        # 所以把状态和一键启动直接摆在输出设备下面。
        brf = ttk.Frame(p)
        brf.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        brf.columnconfigure(1, weight=1)
        ttk.Label(brf, text="声卡桥").grid(row=0, column=0, sticky="w")
        self.lb_bridge = ttk.Label(brf, text="未运行", foreground=theme.FG_DIM)
        self.lb_bridge.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.btn_bridge = ttk.Button(brf, text="一键启动", style="Tool.TButton",
                                     command=self._bridge_toggle)
        self.btn_bridge.grid(row=0, column=2, sticky="e")
        ttk.Label(p, style="Faint.TLabel",
                  text="输出设备选 CABLE Input 时必须开着它，声音才会走到 FPGA→音响",
                  wraplength=700, justify="left").grid(row=7, column=0,
                                                       columnspan=4, sticky="w")

        self.lb_rt_state = ttk.Label(p, text="状态：未启动", foreground=theme.FG_DIM)
        self.lb_rt_state.grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Label(p, text="输出电平").grid(row=9, column=0, sticky="w", pady=(8, 0))
        self.pb_level = ttk.Progressbar(p, orient="horizontal", length=340,
                                        mode="determinate", maximum=100)
        self.pb_level.grid(row=9, column=1, columnspan=3, sticky="ew", pady=(8, 0))

        self.lb_rt_stat = ttk.Label(p, text="", foreground=theme.FG,
                                    font=theme.FONT_MONO)
        self.lb_rt_stat.grid(row=10, column=0, columnspan=4, sticky="w", pady=(6, 0))

        ttk.Label(p, style="Faint.TLabel",
                  text="延迟主要由块大小决定：1024 点 @48 kHz ≈ 21 ms/块，"
                       "加上约 3 块的预填充，整体约 100~150 ms。",
                  wraplength=700, justify="left").grid(row=11, column=0,
                                                       columnspan=4, sticky="w",
                                                       pady=(8, 0))



    def _send_age(self, quiet=False):
        """把「源年龄 → 目标年龄」换成参数包，发给 FPGA 并回显算出的参数。

        quiet=True 用于切换引擎时自动下发：不在日志里刷屏，但参数回显照旧
        （参数有没有真的送到板子，必须让人一眼看到，不能只在日志里）。
        """
        try:
            import age_link
            f = int(self.v_age_from.get())
            t = int(self.v_age_to.get())
        except Exception as e:
            self.lb_age.configure(text="年龄要填整数：%r" % (e,))
            return
        sex = "f" if self.v_age_sex.get() == "女" else "m"
        try:
            pk, p = age_link.packet_from_ages(f, t, sex)
        except Exception as e:
            self.lb_age.configure(text="参数换算失败：%r" % (e,))
            if not quiet:
                self._log("年龄变声：参数换算失败 %r" % (e,))
            return
        port = (self.v_age_port.get() or "").strip()
        ok, msg = age_link.send(pk, port or None)
        txt = ("%d→%d 岁  变调 r=%.3f  共振峰 β=%.3f  气声 %.2f  微颤 %.1f  "
               "倾斜 %+.1f dB" % (f, t, p["r"], p["beta"], p.get("breath", 0.0),
                                  p.get("shimmer", 0.0), p.get("tilt_db", 0.0)))
        if ok:
            head = "已下发到 %s · " % port
        elif not port:
            head = "仅本机生效（未填串口）· "
        else:
            head = "未下发（%s）· " % msg
        self.lb_age.configure(text=head + txt)
        if not quiet:
            self._log("年龄变声：" + txt)
            if not ok and port:
                self._log("年龄变声：" + msg)

    # ------------------------------------------------------------------
    def _enum_devices(self):
        """枚举输入/输出设备，并**把打不开的剔除掉**。

        ★ 这是 "permission denied" 的主因：Windows 上 PortAudio 会列出
          WDM-KS 那一批设备（名字很正经，比如 "Speakers 1 (Realtek HD
          Audio output with HAP)"），但它们**一条都打不开** —— 报错是
          "Unanticipated host error"，在部分驱动/语言环境下就显示成
          "permission denied"。用户以为"没人占用啊"，其实是选了个
          根本不支持共享模式的条目。
          实测本机 17 个输出里 6 个、13 个输入里 7 个属于这一类。

        ★ 为什么不用 sd.check_output_settings() 预筛：它对 WDM-KS 一律
          返回 OK，只有真开一次才准（见 audio_io.probe_stream 的说明）。

        被剔除的设备会打进日志，免得用户以为"我的声卡消失了"。
        """
        ins = audio_io.list_devices("input")
        outs = audio_io.list_devices("output")
        ins, drop_in = audio_io.usable_devices(ins, "input", channels=1)
        outs, drop_out = audio_io.usable_devices(outs, "output", channels=2)
        self._in_idx = [d[0] for d in ins]
        self._out_idx = [d[0] for d in outs]
        for kind, dropped in (("输入", drop_in), ("输出", drop_out)):
            for idx, name, host in dropped:
                self._log("设备不可用已剔除（%s）：%s [%s] —— 该接口不支持共享模式打开"
                          % (kind, name, host))
        return ins, outs

    def _refresh_devices(self):
        """热插拔之后重新枚举。运行中不刷新，避免动到正在用的流。"""
        if self.rt.running:
            messagebox.showinfo("正在运行", "实时变声运行中，请先停止再刷新。")
            return
        ins, outs = self._enum_devices()
        in_names = ["%s   [%s]" % (d[1], d[2]) for d in ins] or ["（无输入设备）"]
        out_names = ["%s   [%s]" % (d[1], d[2]) for d in outs] or ["（无输出设备）"]
        keep_in, keep_out = self.cb_in.get(), self.cb_out.get()
        self.cb_in.configure(values=in_names)
        self.cb_out.configure(values=out_names)
        self.cb_in.set(keep_in if keep_in in in_names else
                       (in_names[0] if in_names else ""))
        self.cb_out.set(keep_out if keep_out in out_names else
                        (out_names[0] if out_names else ""))
        self._log("设备列表已刷新：输入 %d 个 / 输出 %d 个"
                  % (len(in_names), len(out_names)))

    # ------------------------------------------------------------------
    #  声卡桥：CABLE Input → FPGA → STM32 → 音响
    #  这一段跑在独立进程（tool/fpga_audio_bridge.py）里，本进程只负责
    #  "让你看见它在不在" 和 "一键把它拉起来"。
    # ------------------------------------------------------------------
    def _bridge_script(self):
        root = _project_root()
        if not root:
            return None
        p = os.path.join(root, "tool", "fpga_audio_bridge.py")
        return p if os.path.isfile(p) else None

    def _bridge_alive(self):
        """(在跑吗, 显示文字)。

        自己拉起来的看进程句柄；别人（双击 bat）拉起来的看串口被谁占着 ——
        pyserial 打开串口是独占的，打不开就说明有桥在用它。
        """
        if self._bridge_proc is not None and self._bridge_proc.poll() is None:
            return True, "运行中（PID %d）" % self._bridge_proc.pid
        port = _bridge_ch340_port()
        if port:
            try:
                import serial
                s = serial.Serial(port, timeout=0.05)
                s.close()
                return False, "未运行"
            except Exception:
                return True, "运行中（外部启动）"
        return False, "未运行（没找到 CH340）"

    def _bridge_toggle(self):
        alive, _t = self._bridge_alive()
        if alive:
            self._bridge_stop()
        else:
            self._bridge_start()

    def _bridge_start(self):
        script = self._bridge_script()
        if not script:
            messagebox.showerror("声卡桥", "找不到 tool/fpga_audio_bridge.py")
            return

        if not self._bridge_py:
            self._bridge_py = _find_bridge_python()

        if not self._bridge_py:
            # 打包后的 exe 里 sys.executable 是 exe 本身，跑不了 .py。
            # 找不到真解释器就退回启动脚本（它里面写死了本机 python 路径）。
            bat = os.path.join(os.path.dirname(script), "启动FPGA声卡桥.bat")
            if os.path.isfile(bat):
                try:
                    os.startfile(bat)
                    self._log("声卡桥：已用启动脚本拉起（另开一个窗口）")
                    return
                except Exception as e:
                    self._log("声卡桥启动失败：%r" % (e,))
                    return
            messagebox.showerror(
                "声卡桥",
                "没找到装了 pyserial/numpy/sounddevice 的 Python。\n"
                "可以设环境变量 ACX720_PYTHON 指向它，或直接双击\n"
                "tool\\启动FPGA声卡桥.bat")
            return

        try:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) \
                if os.name == "nt" else 0
            self._bridge_proc = subprocess.Popen(
                [self._bridge_py, "-u", script],
                cwd=os.path.dirname(script), creationflags=flags)
            self._log("声卡桥：已启动（PID %d，%s）"
                      % (self._bridge_proc.pid, self._bridge_py))
        except Exception as e:
            self._log("声卡桥启动失败：%r" % (e,))

    def _bridge_stop(self):
        p = self._bridge_proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
            self._log("声卡桥：已停止")
        else:
            self._bridge_proc = None
            messagebox.showinfo(
                "声卡桥", "这个桥不是本程序启动的，去它自己的窗口按 Ctrl+C "
                          "或直接关掉那个窗口。")

    def _bridge_refresh(self):
        """刷新界面上的桥状态。由 _poll() 每 BRIDGE_POLL_SEC 秒调一次。"""
        try:
            alive, txt = self._bridge_alive()
            owned = (self._bridge_proc is not None
                     and self._bridge_proc.poll() is None)
            self.lb_bridge.configure(
                text=txt, foreground=theme.OK if alive else theme.FG_DIM)
            if alive and not owned:
                self.btn_bridge.configure(text="外部运行中", state="disabled")
            else:
                self.btn_bridge.configure(
                    text="停止" if alive else "一键启动", state="normal")
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _build_file_tab(self, p):
        p.columnconfigure(1, weight=1)

        ttk.Label(p, text="输入文件").grid(row=0, column=0, sticky="w")
        ttk.Entry(p, textvariable=self.v_infile).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(p, text="浏览…", command=self._pick_in).grid(row=0, column=2)

        ttk.Label(p, text="输出文件").grid(row=1, column=0, sticky="w")
        ttk.Entry(p, textvariable=self.v_outfile).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(p, text="另存为…", command=self._pick_out).grid(row=1, column=2, pady=(6, 0))

        self.lb_file_info = ttk.Label(p, text="尚未载入文件", foreground=theme.FG_DIM)
        self.lb_file_info.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ★ 默认勾上。主输出文件是「左声道人声 / 右声道伴奏」——它本来就不是
        #   一条"干净的人声"，用音箱播时两路在空中直接混回去，用耳机则是
        #   左耳人声右耳伴奏，主观上非常像"分离得不干净"。
        #   分离模块要的本来就是两条独立音轨，所以默认就给出来，
        #   别让人拿着一个双轨文件去判断分离质量。
        self.v_export_stems = tk.BooleanVar(value=True)
        # 两个开关放在同一个容器里竖排，省得为了加一行去改后面所有控件的行号
        opts = ttk.Frame(p)
        opts.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.cb_stems = ttk.Checkbutton(
            opts, variable=self.v_export_stems,
            text="同时导出人声 / 伴奏两条独立音轨（分离模块建议勾上："
                 "主输出本身是「左人声 / 右伴奏」双轨文件）")
        self.cb_stems.pack(anchor="w")
        self.cb_howl = ttk.Checkbutton(
            opts, variable=self.v_anti_howl,
            text="防啸叫（声学反馈抑制）：外放时压住「人声→喇叭→再被麦克风"
                 "收回来」形成的自激。只在该频点挂陷波，正常说话不动")
        self.cb_howl.pack(anchor="w", pady=(2, 0))

        btns = ttk.Frame(p)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.btn_proc = ttk.Button(btns, text="开始处理", style="Accent.TButton",
                                   command=self._do_process, state="disabled")
        self.btn_proc.pack(side="left")
        ttk.Button(btns, text="试听原声", style="Tool.TButton",
                   command=self._play_source).pack(side="left", padx=8)
        ttk.Button(btns, text="播放结果", style="Tool.TButton",
                   command=self._play_result).pack(side="left")
        ttk.Button(btns, text="卸载素材", style="Tool.TButton",
                   command=self._unload_source).pack(side="left", padx=8)

        # 内嵌播放器：播放结果 / 原声，进度、音量、左右声道开关都在这一行
        self.player_bar = playermod.PlayerBar(p, self.player, log=self._log)
        self.player_bar.grid(row=5, column=0, columnspan=3, sticky="ew",
                             pady=(6, 0))

        pf = ttk.Frame(p)
        pf.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        pf.columnconfigure(1, weight=1)
        ttk.Label(pf, text="处理进度", style="Faint.TLabel").grid(row=0, column=0,
                                                                 sticky="w")
        self.pb_file = ttk.Progressbar(pf, orient="horizontal", length=400,
                                       mode="determinate", maximum=100)
        self.pb_file.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self.lb_file_state = ttk.Label(p, text="", foreground=theme.FG,
                                       justify="left", wraplength=820)
        self.lb_file_state.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # 说明文字**只留三行要点**。早期版本在这里堆了 11 行，把这一页撑到
        # 436 px，右栏整体 995 px，900 px 的窗口里 Notebook 被压成 115 px，
        # 「开始处理」按钮直接被挤没了。完整说明在 host/README_HOST.md。
        tip = ttk.Label(p, justify="left", foreground=theme.FG_DIM,
                        font=theme.FONT_SMALL,
                        text=(
            "输入：WAV / FLAC / MP3 / M4A / AAC / OGG / OPUS / AIFF / WMA"
            "（自动挑后端，读不了会说明原因）\n"
            "输出：按扩展名决定 —— .wav（16bit）/ .flac（无损）/ .ogg / .aiff\n"
            "「分离」输出：左声道=人声、右声道=伴奏，可另导出两条音轨；"
            "两种引擎（中置提取 / demucs）语义一致"))
        tip.grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 0))

    # ------------------------------------------------------------------
    def _build_log(self):
        f = theme.card(self, "日志")
        f.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=0)

        # 工具条：缓存/日志都是会随时间长大的东西，给一个"立刻清"的入口，
        # 旁边显示自动巡检的状态，长时间挂机时一眼能看出它在干活。
        tb = ttk.Frame(f)
        tb.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        ttk.Button(tb, text="立即清理缓存", style="Tool.TButton",
                   command=self._clean_caches_now).pack(side="left")
        ttk.Button(tb, text="清空日志", style="Tool.TButton",
                   command=self._clear_log).pack(side="left", padx=6)
        self.lb_cache = ttk.Label(tb, text="缓存：—", foreground=theme.FG_DIM,
                                  font=theme.FONT_SMALL)
        self.lb_cache.pack(side="left", padx=(10, 0))
        ttk.Label(tb, style="Faint.TLabel",
                  text="（每 %d 秒自动巡检：裁日志、释放闲置超过 %d 分钟的结果缓存、"
                       "卸载闲置超过 %d 分钟的素材、总量超 %d MB 立即清较早的、衰减削波计数）"
                  % (HOUSEKEEP_SEC, PROC_TTL_SEC // 60, SRC_TTL_SEC // 60,
                     int(CACHE_AUTO_MB))
                  ).pack(side="left", padx=(6, 0))

        # tk.Text 不是 ttk 控件，不吃主题，必须显式给配色
        self.log = tk.Text(f, height=5, font=theme.FONT_MONO, wrap="word",
                           bg=theme.BG, fg=theme.FG_DIM,
                           insertbackground=theme.ACCENT,
                           selectbackground=theme.ACCENT_DK,
                           selectforeground=theme.FG,
                           relief="flat", highlightthickness=1,
                           highlightbackground=theme.BORDER,
                           highlightcolor=theme.ACCENT_DK, padx=6, pady=4)
        ys = ttk.Scrollbar(f, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.grid(row=1, column=0, sticky="ew")
        ys.grid(row=1, column=1, sticky="ns")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------
    #  缓存与巡检（长时间稳定运行）
    # ------------------------------------------------------------------
    @staticmethod
    def _mb(nbytes):
        return nbytes / (1024.0 * 1024.0)

    def _cache_bytes(self):
        """当前我们真正持有的音频缓冲总量（不含声卡与 tkinter 内部）。"""
        b = 0
        seen = set()
        for a in (self.file_data, self.proc_data, self._play_data,
                  getattr(self.player, "data", None)):
            if a is None or id(a) in seen:
                continue
            seen.add(id(a))
            try:
                b += int(np.asarray(a).nbytes)
            except Exception:
                pass
        return b

    def _trim_log(self, force=False):
        """日志只保留最近 LOG_MAX_LINES 行，超出的从最旧开始丢。

        返回丢掉的行数。这是"自动清掉很久以前的记录"最直接的一环。
        """
        limit = LOG_MAX_LINES if force else LOG_TRIM_AT
        if self._log_lines <= limit:
            return 0
        try:
            n = int(self.log.index("end-1c").split(".")[0])
        except Exception:
            return 0
        if n <= LOG_MAX_LINES:
            self._log_lines = n
            return 0
        drop = n - LOG_MAX_LINES
        self.log.configure(state="normal")
        self.log.delete("1.0", "%d.0" % (drop + 1))
        self.log.configure(state="disabled")
        self._log_lines = LOG_MAX_LINES
        return drop

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._log_lines = 0

    def _release_proc_data(self, why):
        """释放处理结果缓存（可以靠重跑再生，是唯一值得自动回收的大块）。"""
        if self.proc_data is None:
            return 0
        if self.player.playing and self.player.data is self.proc_data:
            self._log("处理结果正在播放，暂不释放（停止播放后由巡检回收）")
            return 0
        if self.player.data is self.proc_data:
            self.player.unload()
            self._play_data = None
            self.player_bar.refresh()
        n = int(np.asarray(self.proc_data).nbytes)
        self.proc_data = None
        self._proc_split_info = None
        self._log("已释放处理结果缓存（%s，%.1f MB）——需要时重新点「开始处理」即可"
                  % (why, self._mb(n)))
        return n

    def _release_source(self, why, manual=False):
        """卸载载入的素材。自动卸载只发生在它闲置很久且没有在跑处理时。"""
        if self.file_data is None:
            return 0
        if self.player.playing and self.player.data is self.file_data:
            self._log("原声正在播放，暂不卸载（停止播放后再试）")
            return 0
        if self.player.data is self.file_data or self.player.data is self.proc_data:
            self.player.unload()
            self._play_data = None
            self.player_bar.refresh()
        n = int(np.asarray(self.file_data).nbytes)
        path = self.file_path
        self.file_data = None
        self.file_path = ""
        self.proc_data = None
        self._proc_split_info = None
        self._proc_at = 0.0
        for w in (self.btn_proc,):
            try:
                w.configure(state="disabled")
            except Exception:
                pass
        try:
            self.lb_file_info.configure(
                text="（素材已卸载：%s）\n需要时重新「浏览…」载入" % why)
            self.lb_file_state.configure(text="")
            self.pb_file["value"] = 0
            self.spec.clear_band("in")
            self.spec.clear_band("out")
            self.spec.draw()
        except Exception:
            pass
        self._log("%s素材缓存：%.1f MB 已释放（%s）%s"
                  % ("手动卸载" if manual else "自动卸载", self._mb(n), why,
                     ("，原文件：" + os.path.basename(path)) if path else ""))
        return n

    def _clean_caches_now(self, silent=False):
        """手动/自动清理：裁日志 + 释放闲置的结果缓存与素材。

        返回 (释放的字节数, 裁掉的行数)。
        """
        dropped = self._trim_log(force=True)
        freed = 0
        now = time.time()
        if (self.proc_data is not None and self._proc_at > 0
                and (now - self._proc_at) > PROC_TTL_SEC):
            freed += self._release_proc_data("闲置超过 %d 分钟" % (PROC_TTL_SEC // 60))
        # 素材比结果金贵，闲置阈值长得多，而且处理中绝不碰
        if (self.file_data is not None and not self._processing
                and self._file_at > 0 and (now - self._file_at) > SRC_TTL_SEC):
            freed += self._release_source("闲置超过 %d 分钟" % (SRC_TTL_SEC // 60))
        # ★ 硬阈值兜底：不管闲不闲置，总量积累超过 CACHE_AUTO_MB 就按
        #   "较早的先清"的顺序回收——处理结果（_proc_at 更早、可重新生成）
        #   先于素材；素材也压不回来时才动素材。处理中绝不碰。
        total = self._mb(self._cache_bytes())
        if total > CACHE_AUTO_MB and not self._processing:
            if self.proc_data is not None:
                freed += self._release_proc_data(
                    "缓存总量 %.0f MB 超过 %d MB，自动清理较早的处理结果"
                    % (total, int(CACHE_AUTO_MB)))
            total = self._mb(self._cache_bytes())
            if total > CACHE_AUTO_MB and self.file_data is not None:
                freed += self._release_source(
                    "缓存总量 %.0f MB 仍超过 %d MB，继续清理较早的素材"
                    % (total, int(CACHE_AUTO_MB)))
            # 播放缓冲也在缓存总量里：结果和素材都清完仍超、且当前没在
            # 播放，就把残留的播放缓冲也放掉——别让一个死缓冲顶住黄灯
            total = self._mb(self._cache_bytes())
            if total > CACHE_AUTO_MB and not self.playing \
                    and self._play_data is not None:
                n = int(np.asarray(self._play_data).nbytes)
                self._play_data = None
                freed += n
                self._log("已释放播放缓冲 %.1f MB（当前不在播放）" % self._mb(n))
        if freed > 0:
            self._hk_cleaned += 1
            self._hk_last = "释放 %.1f MB" % self._mb(freed)
        if not silent:
            self._log("缓存已清理：释放 %.1f MB，日志裁掉 %d 行（当前缓存 %.1f MB）"
                      % (self._mb(freed), dropped, self._mb(self._cache_bytes())))
        self._update_cache_label()
        return freed, dropped

    def _update_cache_label(self):
        b = self._cache_bytes()
        txt = "缓存：%.1f MB" % self._mb(b)
        if self._hk_cleaned:
            txt += "   已自动清理 %d 次（%s）" % (self._hk_cleaned, self._hk_last or "—")
        run = time.time() - self._started_at
        txt += "   运行 %s" % self._dur_str(run)
        try:
            self.lb_cache.configure(text=txt,
                                    foreground=theme.ERR if self._mb(b) > CACHE_WARN_MB
                                    else theme.FG_DIM)
        except Exception:
            pass

    @staticmethod
    def _dur_str(sec):
        sec = int(sec)
        d, sec = divmod(sec, 86400)
        h, sec = divmod(sec, 3600)
        m, s = divmod(sec, 60)
        if d:
            return "%d 天 %d 小时" % (d, h)
        if h:
            return "%d 小时 %d 分" % (h, m)
        if m:
            return "%d 分 %d 秒" % (m, s)
        return "%d 秒" % s

    def _start_housekeep(self):
        self._housekeep()
        self.after(HOUSEKEEP_SEC * 1000, self._start_housekeep)

    def _housekeep(self):
        """周期巡检：日志裁行、闲置缓存回收、缓存状态刷新、定期状态报告。

        这些都是"会随时间长大的东西"，长时间挂机就靠它兜住。
        """
        self._hk_tick += 1
        try:
            self._trim_log()
            self._clean_caches_now(silent=True)
            self._update_cache_label()
        except Exception as e:
            self._log("巡检异常（已忽略）：%r" % (e,))
        if self._hk_tick % CACHE_LOG_EVERY == 0:
            rt = self.rt
            self._log("运行 %s ｜ 缓存 %.1f MB ｜ 已自动清理 %d 次 ｜ "
                      "实时：缓冲 %d 块 / 溢出 %d / 欠载 %d ｜ 累计削波 入 L%d R%d、出 L%d R%d"
                      % (self._dur_str(time.time() - self._started_at),
                         self._mb(self._cache_bytes()), self._hk_cleaned,
                         rt.load(), rt.overflow, rt.underrun,
                         rt.clip_in_total[0], rt.clip_in_total[1],
                         rt.clip_out_total[0], rt.clip_out_total[1]))

    # ------------------------------------------------------------------
    #  参数 -> 配置
    # ------------------------------------------------------------------
    def _refresh_cfg(self):
        """在主线程刷新参数快照。由 _poll 每轮调用（GUI 线程，安全）。"""
        try:
            cfg = self.current_cfg()
        except Exception:
            return
        with self._cfg_lock:
            self._cfg_snap = cfg

    # ------------------------------------------------------------------
    def _publish_mode(self):
        """把「模块 × 引擎 × 关键参数」写进 tool/logs/mode.json。

        声卡桥会读这个文件，转成 16 字节配置包发到串口；单片机收下之后
        刷新那块 0.96" OLED。只在内容变化时写（原子替换，桥读不到半个文件）。

        ★ 任何异常都吞掉：这只是给外接屏同步用的，绝不能影响变声本身；
          目录不可写（受限进程）时就当没有这个功能，声音照常。
        """
        now = time.time()
        if now - self._mode_t < 0.3:
            return
        self._mode_t = now
        try:
            with self._cfg_lock:
                cfg = self._cfg_snap
        except Exception:
            cfg = None
        if not cfg:
            return
        try:
            mod = cfg.get("module", dsp.MODULE_PITCH)
            eng = cfg.get("engine", dsp.ENGINE_FPGA_DIRECT)
            dph = int(cfg.get("dphase", 0))
            if mod == dsp.MODULE_SPLIT:
                eng_s = "local" if eng == dsp.ENGINE_LOCAL_MODEL else "center"
            else:
                eng_s = ("fpga_model" if eng == dsp.ENGINE_FPGA_MODEL
                         else "fpga_direct")
            gain = max(1e-6, float(cfg.get("gain", 1.0)))
            gdb = int(round(20.0 * math.log10(gain)))
            gdb = max(-60, min(12, gdb))
            payload = {
                "m": "split" if mod == dsp.MODULE_SPLIT else "pitch",
                "e": eng_s,
                "semi": round(float(cfg.get("semi_exact", 0.0)), 2),
                "dph": dph,
                "ratio": round(1.0 + dph / 64.0, 4),
                "vg": int(round(float(cfg.get("vocal_gain", 1.0)) * 100.0)),
                "ag": int(round(float(cfg.get("acc_gain", 1.0)) * 100.0)),
                "g": gdb,
                "src": "file" if cfg.get("source") == dsp.SRC_FILE else "live",
            }
            txt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if txt == self._mode_txt:
                return
            self._mode_txt = txt
            path = _mode_json_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(txt)
            os.replace(tmp, path)
        except Exception:
            pass

    def _cfg_get(self):
        """给 RealtimeEngine 的回调源：返回纯 Python 快照，不含 Tk 对象。"""
        with self._cfg_lock:
            return self._cfg_snap

    def _ai_quality_key(self):
        if local_ai is None:
            return "std"
        ks = [k for k, _ in local_ai.QUALITY_NAMES]
        i = max(0, min(self.v_ai_q.get(), len(ks) - 1))
        return ks[i]

    def _ai_voice_key(self):
        if local_ai is None:
            return "hq"
        ks = [k for k, _ in local_ai.VOICE_NAMES]
        i = max(0, min(self.v_ai_voice.get(), len(ks) - 1))
        return ks[i]

    def _ai_device_key(self):
        return ["auto", "cuda", "cpu"][max(0, min(self.v_ai_dev.get(), 2))]

    def current_cfg(self):
        """读当前界面参数成配置字典。

        ★ 只能从主线程调用：里面会回写 v_engine / v_mode 两个 Tk 变量。
        音频线程要拿参数请用 _cfg_get()（主线程刷好的快照）。
        """
        # 写 Tk 变量的动作只在主线程做；万一被别的线程调用（历史 bug 就是
        # 音频回调调了它），退化成只读，不再往回 set。
        can_write = (threading.current_thread() is self._main_thread)
        module = self.v_module.get()
        engine = self.v_engine.get()
        # 变调下有两个引擎：FPGA 直算 / 年龄变声。旧配置里若存了已下线的
        # FPGA_MODEL，这里强制回退到直算（不然会走到不存在的实现）。
        if module == dsp.MODULE_PITCH and engine not in (
                dsp.ENGINE_FPGA_DIRECT, dsp.ENGINE_AGE):
            engine = dsp.ENGINE_FPGA_DIRECT
            if can_write:
                try:
                    self.v_engine.set(engine)
                except Exception:
                    pass
        # v_mode 是给"板对照 + 旧测试"看的兼容量：由 模块×引擎 反推
        if can_write:
            if module == dsp.MODULE_PITCH:
                self.v_mode.set(dsp.MODE_PITCH)
            elif engine == dsp.ENGINE_LOCAL_MODEL:
                self.v_mode.set(dsp.MODE_AI_LOCAL)
            else:
                self.v_mode.set(dsp.MODE_SPLIT)
        # "文件处理用高质量算法"这个勾选项在新 UI 里由引擎承担：
        # 选了「FPGA 内部小模型」就是 HQ，选了「FPGA 直算」就是延迟线。
        hq_file = (module == dsp.MODULE_PITCH and
                   engine == dsp.ENGINE_FPGA_MODEL)
        # 年龄变声：变调量由**年龄**换算，不是滑杆。
        #   ★ 这一条不做的话，选了"年龄变声"本机听到的仍是滑杆的 0 半音，
        #     年龄参数只发给了板子 —— 用户会以为"选了没反应"。
        #   换算走 host/age_model.py，与下发到板子的 dphase 同源，保证
        #   "本机听到的"和"板子做的"是同一个比例。
        age_semi = None
        if module == dsp.MODULE_PITCH and engine == dsp.ENGINE_AGE:
            try:
                import age_model as _AM
                _p = _AM.params(int(self.v_age_from.get()),
                                int(self.v_age_to.get()),
                                "f" if self.v_age_sex.get() == "女" else "m")
                age_semi = float(12.0 * __import__("math").log2(_p["r"]))
            except Exception as _e:
                if can_write:
                    try:
                        self.lb_age.configure(
                            text="年龄参数换算失败：%r（本次按 0 半音处理）" % (_e,))
                    except Exception:
                        pass
        semi = age_semi if age_semi is not None else float(self.v_semi.get())

        return dict(
            mode=self.v_mode.get(),
            module=module,
            engine=engine,
            source=self.v_source.get(),
            anti_howl=bool(self.v_anti_howl.get()),
            dphase=dsp.semitone_to_dphase(semi),
            semi_exact=float(semi),    # 未经板上档位量化的原始半音数
            formant=float(self.v_formant.get()) / 100.0,
            hq_file=hq_file,
            ring_freq=self.v_ring.get(),
            echo_taps=self.v_taps.get(),
            echo_gain=self.v_egain.get(),
            gain=self.v_gain.get(),
            ingain=10.0 ** (self.v_ingain.get() / 20.0),
            # 分离的"人声 / 伴奏"两路增益：两个引擎共用同一组滑条语义，
            # 但参数框各自有一套控件（本地模型那套还要做人声变调）。
            vocal_gain=float(self.v_ai_vgain.get() / 100.0
                             if engine == dsp.ENGINE_LOCAL_MODEL
                             else self.v_sp_vgain.get() / 100.0),
            acc_gain=float(self.v_ai_again.get() / 100.0
                           if engine == dsp.ENGINE_LOCAL_MODEL
                           else self.v_sp_again.get() / 100.0),
            split=dict(
                depth=self.v_sp_depth.get() / 100.0,
                f0_hz=float(self.v_sp_thr.get()),
                swap=bool(self.v_sp_swap.get()),
                broadband=self.v_sp_bb.get() / 100.0,
                harmonic=bool(self.v_sp_harm.get()),
                bass_protect_hz=float(self.v_sp_bass.get()),
                # 唯一语义：左=人声、右=伴奏（不再有 route 参数）
            ),
            ai=dict(
                use_demucs=True,
                quality=self._ai_quality_key(),
                device=self._ai_device_key(),
                vocal_gain=float(self.v_ai_vgain.get()) / 100.0,
                acc_gain=float(self.v_ai_again.get()) / 100.0,
            ),
        )

    # ------------------------------------------------------------------
    def _on_module_change(self):
        """切模块：换引擎单选组、换参数框。"""
        mod = self.v_module.get()
        if mod == dsp.MODULE_PITCH:
            self.f_eng_pitch.grid()
            self.f_eng_split.grid_remove()
            self.v_engine.set(dsp.ENGINE_FPGA_DIRECT)
            self._show_frames(self.f_pitch, self.f_gain, self.f_sw)
        else:
            self.f_eng_split.grid()
            self.f_eng_pitch.grid_remove()
            self.v_engine.set(dsp.ENGINE_FPGA_DIRECT)
            self._show_frames(self.f_split_fpga, self.f_gain, self.f_sw)
        self._on_engine_change()

    def _on_engine_change(self):
        """切引擎：只换参数框，链的状态在真正开始跑时才重建。"""
        mod = self.v_module.get()
        eng = self.v_engine.get()
        if mod == dsp.MODULE_PITCH and eng == dsp.ENGINE_AGE:
            self._show_frames(self.f_age, self.f_gain, self.f_sw)
            self._log("引擎：%s" % dsp.ENGINE_NAMES[(mod, eng)])
            # 切到这个引擎就把当前年龄参数下发一次，省得还要再点按钮
            self._send_age(quiet=True)
        elif mod == dsp.MODULE_PITCH:
            self._show_frames(self.f_pitch, self.f_gain, self.f_sw)
            self._log("引擎：%s" % dsp.ENGINE_NAMES[(mod, eng)])
        elif eng == dsp.ENGINE_LOCAL_MODEL:
            self._show_frames(self.f_split_local, self.f_gain, self.f_sw)
            self._log("引擎：%s" % dsp.ENGINE_NAMES[(mod, eng)])
            # 选到本地模型即自动准备 AI 运行环境（挂载 torch + 下载权重）
            if local_ai is not None:
                self._prepare_ai()
            ok, why = (local_ai.demucs_ready() if local_ai is not None
                       else (False, "local_ai 模块缺失"))
            if not ok:
                self._log("提醒：%s。点「准备 AI 环境」或勾选上面的开关即可自动准备；"
                          "准备好前该引擎会退化为直通（原样输出），不会崩。" % why)
        else:
            self._show_frames(self.f_split_fpga, self.f_gain, self.f_sw)
            self._log("引擎：%s" % dsp.ENGINE_NAMES[(mod, eng)])
        self._refresh_labels()

    def _on_source_change(self):
        src = self.v_source.get()
        if src == dsp.SRC_REALTIME:
            self._log("输入：实时变声（麦克风 → 效果链 → 音箱）")
        else:
            self._log("输入：文件变声（选一个音频文件，整段处理完存盘）")
        try:
            self.nb.select(1 if src == dsp.SRC_FILE else 0)
        except Exception:
            pass

    def _on_sp_thr(self):
        """把「自动聚类」或某个固定 Hz 值写回 v_sp_thr（旧控件，兼容保留）。"""
        i = self.cb_sp_thr.current()
        vals = [0] + [120, 140, 155, 165, 180, 200, 220]
        self.v_sp_thr.set(vals[max(0, min(i, len(vals) - 1))])

    def _on_sp_route(self):
        """两个参数框里的「声道路由」共用同一个 v_sp_swap，选哪个都一样。"""
        swap = bool(self.cb_sp_route.current()) if hasattr(self, "cb_sp_route") else False
        self.v_sp_swap.set(swap)
        for cb in (getattr(self, "cb_sp_route", None),
                   getattr(self, "cb_ai_route", None)):
            if cb is not None:
                cb.current(1 if swap else 0)
        self._log("分离输出：%s" % ("人声→右 / 伴奏→左" if swap
                                    else "人声→左 / 伴奏→右"))

    def _file_channels(self):
        a = self.file_data
        if a is None:
            return 0
        return 1 if a.ndim == 1 else int(a.shape[1])

    def _on_semi(self):
        pass

    def _reset_params(self):
        self.v_module.set(dsp.MODULE_PITCH)
        self.v_engine.set(dsp.ENGINE_FPGA_DIRECT)
        self.v_source.set(dsp.SRC_REALTIME)
        self.v_mode.set(dsp.MODE_PITCH)
        self.v_semi.set(0.0)
        self.v_formant.set(100.0)
        self.v_ring.set(1)
        self.v_taps.set(8)
        self.v_egain.set(2)
        self.v_gain.set(1.0)
        self.v_ingain.set(12.0)
        self.v_sp_depth.set(100.0)
        self.v_sp_thr.set(0)
        self.v_sp_swap.set(False)
        self.v_sp_bb.set(20.0)
        self.v_sp_harm.set(True)
        self.v_sp_vgain.set(100.0)
        self.v_sp_again.set(100.0)
        self.v_sp_bass.set(120.0)
        self.v_ai_demucs.set(True)
        self.v_ai_q.set(2)
        self.v_ai_dev.set(0)
        self.v_ai_voice.set(1)
        self.v_ai_semi.set(4.0)
        self.v_ai_formant.set(100.0)
        self.v_ai_vgain.set(100.0)
        self.v_ai_again.set(100.0)
        try:
            self._ring_cb.current(1)
            self._echo_cb.current(2)
            self.cb_ai_q.current(1)
            self.cb_ai_dev.current(0)
            self.cb_ai_voice.current(1)
            self._on_sp_route()
        except Exception:
            pass
        self._on_module_change()

    # ------------------------------------------------------------------
    def _refresh_ai_latency(self):
        """参数框里那行「延迟 / 降级」说明，只对「分离 × 计算机本地模型」有意义。

        两种输入方式的差别必须写清楚，不能含混：

          实时  demucs 分块流着算，延迟 = 块长 + 上下文 ≈ 3 s（单块推理
                耗时另算），左出人声、右出伴奏（可对人声再变调）。
          文件  demucs 整段离线算完，同样左人声 / 右伴奏，
                质量最好，和「FPGA 内部模型」的输出格式完全一致。

        ★ 这里不主动调 local_ai.probe()：它第一次要 import torch（3~6 s），
          而这个函数会被滑块的 trace 反复触发。环境自检在启动时由后台线程
          做（_start_ai_probe），没查完就只显示"自检中"。
        """
        if not hasattr(self, "lb_ai_lat"):
            return
        if self.v_module.get() != dsp.MODULE_SPLIT or \
                self.v_engine.get() != dsp.ENGINE_LOCAL_MODEL:
            self.lb_ai_lat.configure(text="", foreground=theme.WARN)
            return
        if local_ai is None:
            self.lb_ai_lat.configure(
                text="local_ai 模块没能加载（%s）：两种输入方式都会退化为直通，"
                     "不崩，但也没有分离效果。" % LOCAL_AI_WHY,
                foreground=theme.WARN)
            return
        if not self._ai_probed:
            self.lb_ai_lat.configure(
                text="环境自检中…（首次要加载 torch，约几秒）",
                foreground=theme.WARN)
            return
        ok, why = local_ai.demucs_ready()
        if not ok:
            # ★ 单文件 EXE 里刻意没带 torch（cu128 全套 4.2 GB，打进去会让
            #   程序变 4 GB 且每次启动解压几分钟）。所以最常见的"不可用"
            #   原因就是"你跑的是 EXE"。这里要指明出路，不能只报个错。
            hint = ("请用 host\\运行上位机_AI完整版.bat 启动（调本机已装好的 "
                    "torch，支持 GPU 加速）" if getattr(sys, "frozen", False)
                    else "装好后重启本程序即可恢复")
            self.lb_ai_lat.configure(
                text="demucs 不可用（%s）：实时与文件模式都会退化为直通。%s。"
                     % (why, hint),
                foreground=theme.WARN)
            return
        try:
            import local_stream as _ls
            blk, ctx = _ls.DEFAULT_BLOCK_S, _ls.DEFAULT_CTX_S
        except Exception:
            blk, ctx = 1.0, 2.0
        self.lb_ai_lat.configure(
            text="实时：demucs 分块流算，延迟 = 块 %.1f s + 上下文 %.1f s ≈ %.1f s"
                 "（另有单块推理耗时），左出人声、右出伴奏；\n"
                 "文件：整段离线算完，同样左人声 / 右伴奏，质量最好。"
                 % (blk, ctx, blk + ctx),
            foreground=theme.WARN)

    # ------------------------------------------------------------------
    def _refresh_labels(self, *_):
        # 标题栏徽章要先刷：下面 lb_sw 那段在"板上无此组合"时会提前 return，
        # 徽章刷新放在后面就被跳过了（实测切到「分离」模块后徽章一直停在
        # "变调 × FPGA 直算"，是个纯粹的顺序 bug）。
        if hasattr(self, "pill_combo"):
            self._refresh_pills()
        if not hasattr(self, "lb_sw"):
            return
        semi = self.v_semi.get()
        dp = dsp.semitone_to_dphase(semi)
        r = dsp.dphase_to_ratio(dp)
        v = dsp.dphase_to_switch(dp)

        if hasattr(self, "lb_semi"):
            self.lb_semi.configure(text="%+.1f 半音" % semi)
        if hasattr(self, "lb_formant"):
            fo = self.v_formant.get()
            self.lb_formant.configure(
                text="%d%%%s" % (fo, "（留原位）" if fo >= 99.5 else
                                 "（部分随音高）" if fo > 0.5 else "（随音高走）"))
        if hasattr(self, "lb_echo"):
            ms = self.v_taps.get() * 256.0 / 48000.0 * 1000.0
            self.lb_echo.configure(text="%.0f ms  (%d × 256 点)" % (ms, self.v_taps.get()))
        if hasattr(self, "lb_gain"):
            g = self.v_gain.get()
            db = 20.0 * math.log10(g) if g > 0.01 else -40.0
            self.lb_gain.configure(text="%.0f%%   (%+.1f dB)" % (g * 100.0, db))
        if hasattr(self, "lb_ingain"):
            ig = 10.0 ** (self.v_ingain.get() / 20.0)
            self.lb_ingain.configure(text="%.0f dB   (× %.2f)" % (self.v_ingain.get(), ig))
        if hasattr(self, "lb_sp_depth"):
            d = self.v_sp_depth.get()
            self.lb_sp_depth.configure(
                text="%.0f%%%s" % (d, "（人声全取出）" if d >= 99.5 else ""))
        if hasattr(self, "lb_sp_bb"):
            self.lb_sp_bb.configure(text="%.0f%%" % self.v_sp_bb.get())
        if hasattr(self, "lb_sp_vgain"):
            self.lb_sp_vgain.configure(text="%.0f%%" % self.v_sp_vgain.get())
        if hasattr(self, "lb_sp_again"):
            self.lb_sp_again.configure(text="%.0f%%" % self.v_sp_again.get())
        if hasattr(self, "lb_sp_bass"):
            b = self.v_sp_bass.get()
            self.lb_sp_bass.configure(
                text=("关（全频段扣）" if b < 5 else "%.0f Hz" % b))

        # ---- 本地 AI ----
        if hasattr(self, "lb_ai_semi"):
            self.lb_ai_semi.configure(text="%+.1f 半音" % self.v_ai_semi.get())
        if hasattr(self, "lb_ai_formant"):
            fo = self.v_ai_formant.get()
            self.lb_ai_formant.configure(
                text="%d%%%s" % (fo, "（留原位）" if fo >= 99.5 else
                                 "（部分随音高）" if fo > 0.5 else "（随音高走）"))
        if hasattr(self, "lb_ai_vgain"):
            self.lb_ai_vgain.configure(text="%.0f%%" % self.v_ai_vgain.get())
        if hasattr(self, "lb_ai_again"):
            self.lb_ai_again.configure(text="%.0f%%" % self.v_ai_again.get())
        if hasattr(self, "lb_ai_rvc") and local_ai is not None:
            if self.v_ai_voice.get() == 2:
                try:
                    ok, why = local_ai.rvc_status(self.v_ai_rvc_dir.get().strip())
                except Exception as ex:
                    ok, why = False, "自检失败：%s" % ex
                self.lb_ai_rvc.configure(
                    text=("可用：" if ok else "不可用：") + why,
                    foreground=(theme.OK if ok else theme.WARN))
            else:
                self.lb_ai_rvc.configure(
                    text="（RVC 音色转换需自备 .pth 模型，界面暂不提供）",
                    foreground=theme.FG_DIM)
        if hasattr(self, "lb_ai_lat"):
            self._refresh_ai_latency()

        # 板上的 sw 字节：不同模式下低 6 位含义不同。
        # 只有「变调 × FPGA 直算」能一对一招上板子，其余组合都照不到。
        byte = self._sw_byte()
        if byte is None:
            self.lb_sw.configure(
                text="sw = ----（这个组合板上没有，仅上位机有效）",
                foreground=theme.FG_FAINT)
        else:
            self.lb_sw.configure(
                text="sw = %s = 0x%02X" % (format(byte, "08b"), byte),
                foreground=theme.OK)
    # ------------------------------------------------------------------
    #  实时
    # ------------------------------------------------------------------
    def _rt_dev_indices(self):
        ii = self.cb_in.current()
        oo = self.cb_out.current()
        if not self._in_idx or ii < 0:
            return None, None
        idev = self._in_idx[min(ii, len(self._in_idx) - 1)]
        odev = self._out_idx[min(oo, len(self._out_idx) - 1)] if self._out_idx else None
        return idev, odev

    def _rt_toggle(self):
        if self.rt.running:
            self.rt.stop()
            self.btn_rt.configure(text="启动实时变声")
            self.lb_rt_state.configure(text="状态：已停止", foreground=theme.FG_DIM)
            self._log("实时变声已停止")
            return

        # ---- 四种组合都能实时，但延迟与依赖差别很大，启动前如实告知 ----
        # 时域延迟线是采样级延迟；流式相位声码器约 30 ms；demucs 流式是
        # block + ctx ≈ 3 s（单块推理耗时另算）。不把 3 s 说成"实时"。
        mod, eng = self.v_module.get(), self.v_engine.get()
        lat_ms = 0.0
        if mod == dsp.MODULE_PITCH and eng == dsp.ENGINE_FPGA_MODEL:
            lat_ms = 30.0
        elif mod == dsp.MODULE_SPLIT and eng == dsp.ENGINE_LOCAL_MODEL:
            lat_ms = 3000.0
        if lat_ms > 0:
            self._log("算法延迟约 %.0f ms（不含声卡缓冲的约 100 ms）" % lat_ms)

        if mod == dsp.MODULE_SPLIT and eng == dsp.ENGINE_LOCAL_MODEL and \
                local_ai is not None:
            ok, why = local_ai.demucs_ready()
            if not ok and not messagebox.askyesno(
                    "本地模型不可用",
                    "demucs 当前不可用（%s）。\n\n"
                    "「分离 × 计算机本地模型」会退化为直通：不崩、能出声，"
                    "但听到的是没有分离过的原声。\n\n"
                    "仍要启动吗？（选「是」启动直通，选「否」回去装依赖）" % why):
                return

        if not HAVE_SD:
            messagebox.showerror("缺少依赖", "没有 sounddevice，无法访问声卡。")
            return
        idev, odev = self._rt_dev_indices()
        if idev is None or odev is None:
            messagebox.showerror("设备错误", "请选择输入和输出设备。")
            return

        sr = int(self.cb_sr.get())
        blk = int(self.cb_blk.get())
        # 启动前先刷一次快照，保证音频线程第一块就能拿到完整参数，
        # 而不是拿到 {} 走默认配置。
        self._refresh_cfg()
        try:
            self.rt.start(idev, odev, sr, sr, blk, 2, self._cfg_get)
        except Exception as e:
            self._log("启动失败：%r" % (e,))
            messagebox.showerror("启动失败", str(e))
            return

        self.btn_rt.configure(text="停止实时变声")
        extra = ""
        if lat_ms > 0:
            extra = "，算法延迟约 %.0f ms" % lat_ms
        self.lb_rt_state.configure(
            text="状态：运行中（请先小声试一句话）%s" % extra,
            foreground=theme.OK)
        self._log("实时变声启动：%s × %s，输入 dev=%s 输出 dev=%s  %d Hz / %d 点%s"
                  % (dict(dsp.MODULE_NAMES)[mod], dsp.ENGINE_NAMES[(mod, eng)],
                     idev, odev, sr, blk, extra))

    def _rt_reset(self):
        # 走 reset_chain：拿锁 + 清管道，避免和 worker 线程里的
        # chain.configure/process_stream 撞车（原来直接 reset 会撕开状态）
        self.rt.reset_chain()
        self._log("效果链状态已复位（延迟线清零、相位归零）")

    # ------------------------------------------------------------------
    #  文件
    # ------------------------------------------------------------------
    def _pick_in(self):
        p = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频文件（含 FLAC）", audio_io.AUDIO_GLOBS),
                       ("WAV", "*.wav"), ("FLAC 无损", "*.flac"),
                       ("MP3", "*.mp3"), ("M4A / AAC", "*.m4a *.aac"),
                       ("OGG / OPUS", "*.ogg *.oga *.opus"),
                       ("所有文件", "*.*")])
        if not p:
            return
        self.v_infile.set(p)
        self._load_file(p)

    def _load_file(self, p):
        info = {}
        try:
            a, sr = audio_io.load_audio(p, info)
        except Exception as e:
            self._log("读取失败：%s" % str(e).replace("\n", " "))
            messagebox.showerror("读取失败", str(e))
            return
        self.file_data = a
        self.file_sr = sr
        self.file_path = p
        self.proc_data = None
        self._proc_split_info = None
        self._proc_at = 0.0
        self._file_at = time.time()
        n = a.shape[0]
        ch = 1 if a.ndim == 1 else a.shape[1]
        note = ""
        if ch == 1:
            note = "（单声道，处理时会复制成左右两路；分离会退化成谐波提取）"
        elif ch > 2:
            note = "（只取前两路做效果运算，其余声道不参与）"
        self.lb_file_info.configure(
            text="%s\n%d 声道 / %d Hz / %s\n读取后端：%s%s"
                 % (os.path.basename(p), ch, sr, audio_io.duration_str(n, sr),
                    info.get("backend", "?"), ("　" + note) if note else ""))
        base = os.path.splitext(p)[0] + "_变声.wav"
        self.v_outfile.set(base)
        self.btn_proc.configure(state="normal")
        self._stop_play()
        self.player.unload()
        self._play_data = None
        self.player_bar.refresh()
        self.lb_file_state.configure(text="")
        self.pb_file["value"] = 0
        self._log("已载入：%s  (%d Hz, %d ch, %.1f MB, %s 后端, 扩展名 %s)"
                  % (os.path.basename(p), sr, ch, self._mb(np.asarray(a).nbytes),
                     info.get("backend", "?"), info.get("ext") or "无"))
        self._show_file_spectrum(a, self.file_sr, "in")

    def _show_file_spectrum(self, data, sr, which):
        """文件模式：把整段信号画进右上图。

        四路波形视图用整段包络（``QuadScope`` 自己按列取 min/max），
        频谱视图仍然用整段 8 段平均谱 —— 单帧 21 ms 正好落在音节间隙
        会全空。两份数据都更新，切换视图时不用重新算。
        """
        try:
            a = np.asarray(data, dtype=np.float32)
            if a.ndim == 1:
                raw_l, raw_r = a, None
            elif a.shape[1] == 1:
                raw_l, raw_r = a[:, 0], None
            else:
                raw_l, raw_r = a[:, 0], a[:, 1]
        except Exception:
            raw_l = raw_r = None
        try:
            l = self._spec_curve(raw_l, sr) if raw_l is not None else None
            r = self._spec_curve(raw_r, sr) if raw_r is not None else None
        except Exception:
            l = r = None
        l, r = self._spec_pair(l, r)
        self.spec.set_sr(sr)
        self.scope.set_sr(sr)
        if which == "in":
            self.spec.clear_band("out")
            self.spec.set_in(l, r)
            self.scope.set_in(raw_l, raw_r)
        else:
            self.spec.set_out(l, r)
            self.scope.set_out(raw_l, raw_r)
        self.spec.draw()
        self.scope.draw()

    def _pick_out(self):
        p = filedialog.asksaveasfilename(
            title="输出文件", defaultextension=".wav",
            initialfile=os.path.basename(self.v_outfile.get() or "out.wav"),
            filetypes=[("WAV 16bit（默认）", "*.wav"),
                       ("FLAC 无损", "*.flac"),
                       ("OGG Vorbis（有损）", "*.ogg"),
                       ("AIFF", "*.aiff")])
        if p:
            self.v_outfile.set(p)

    def _do_process(self):
        if self.file_data is None:
            return
        out = self.v_outfile.get().strip()
        if not out:
            messagebox.showwarning("未指定输出", "请先指定输出文件路径。")
            return

        mod, eng = self.v_module.get(), self.v_engine.get()

        # ★ 采样率在**开工之前**就定死，别等到写盘才发现是 0 或小数。
        #   读 → 处理 → 写 → 播放器，全程只能有这一个值。
        try:
            sr_fixed = audio_io.norm_sample_rate(self.file_sr)
        except Exception as e:
            messagebox.showerror("采样率无效",
                                 "这个文件的采样率读出来是 %r，无法处理。\n%s"
                                 % (self.file_sr, e))
            return
        if sr_fixed != self.file_sr:
            self._log("采样率归一：%r → %d Hz（写入与播放都用这个值）"
                      % (self.file_sr, sr_fixed))
            self.file_sr = sr_fixed

        self.btn_proc.configure(state="disabled")
        # 开始新处理：先停播；旧结果马上会被新结果替换，顺手卸掉省内存
        self._stop_play()
        if self.player.data is self.proc_data:
            self.player.unload()
            self._play_data = None
            self.player_bar.refresh()
        self.lb_file_state.configure(text="处理中…")
        self.pb_file["value"] = 0
        if mod == dsp.MODULE_SPLIT and eng == dsp.ENGINE_LOCAL_MODEL:
            # AI 链路只有一个文字进度（demucs 会报百分比，变调没有），
            # 凑不出统一的数值，所以用滚动条表示"确实在跑"，细节看下面那行文字
            self.pb_file.configure(mode="indeterminate")
            self.pb_file.start(12)
        else:
            self._pb_stop()
        self._processing = True
        self._progress_pct = -1
        self._file_at = time.time()
        cfg = dict(self.current_cfg())
        cfg["_src"] = self.file_data       # 原样带过去，worker 里再规整成双声道
        cfg["_sr"] = self.file_sr
        cfg["_out"] = out
        cfg["_stems"] = bool(self.v_export_stems.get())
        cfg["_anti_howl"] = bool(self.v_anti_howl.get())
        if cfg["_stems"] and mod != dsp.MODULE_SPLIT:
            self._log("提示：人声/伴奏音轨只在「分离」模块下有效，本次不会导出。")
        if mod == dsp.MODULE_SPLIT:
            front = ("demucs（神经网络，质量高很多）"
                     if eng == dsp.ENGINE_LOCAL_MODEL else "中置提取（纯 DSP）")
            self._log("分离：人声前端 = %s，输出 = 左声道人声 / 右声道伴奏" % front)
        if mod == dsp.MODULE_PITCH and eng == dsp.ENGINE_FPGA_MODEL:
            self._log("文件变调走离线高质量算法：相位声码器 + 共振峰保持，"
                      "整段一次算完，比实时那条流式链路质量更好")
        if mod == dsp.MODULE_SPLIT and eng == dsp.ENGINE_LOCAL_MODEL and \
                local_ai is None:
            messagebox.showerror("本地 AI 不可用",
                                 "local_ai 模块没能加载：%s" % LOCAL_AI_WHY)
            self._pb_stop()
            self.btn_proc.configure(state="normal")
            self.lb_file_state.configure(text="本地 AI 不可用")
            self._processing = False
            return
        t = threading.Thread(target=self._proc_worker, args=(cfg,), daemon=True)
        self._proc_thread = t
        t.start()

    def _proc_worker(self, cfg):
        try:
            if self._abort.is_set():
                return
            data = audio_io.to_stereo(cfg["_src"])   # 单声道这里才复制成两路
            # 全程只认这一个采样率：处理用它、写盘用它、播放器也用它。
            sr = audio_io.norm_sample_rate(cfg["_sr"])
            out = cfg["_out"]
            mod = cfg["module"]
            eng = cfg["engine"]
            want_stems = bool(cfg.get("_stems")) and mod == dsp.MODULE_SPLIT
            t0 = time.time()
            stems = None

            # ---- 按「模块 × 引擎」分发（输入方式在这里恒为文件）----
            if mod == dsp.MODULE_SPLIT:
                # 分离：左声道=人声、右声道=伴奏
                #   前端  提出人声（demucs 或中置提取，由引擎决定）
                #   分配  人声进左、伴奏进右，各自增益独立
                res, stems, split_info = self._split_worker(cfg, data, sr)
            else:
                # 变调 × FPGA 直算：与 rtl/pitch_shift.v 一致的时域延迟线
                split_info = None
                chain = dsp.StereoChain(sr)
                chain.reset()
                chain.configure(mode=dsp.MODE_PITCH, dphase=cfg["dphase"],
                                ring_freq=cfg["ring_freq"], echo_taps=cfg["echo_taps"],
                                echo_gain=cfg["echo_gain"], gain=cfg["gain"])
                pcm_i = audio_io.to_int16(data)
                total = pcm_i.shape[0]
                block = 8192
                outs = []
                done = 0
                for s in range(0, total, block):
                    seg = pcm_i[s:s + block]
                    outs.append(chain.process(seg, block=seg.shape[0]))
                    done += seg.shape[0]
                    pct = int(100.0 * done / max(1, total))
                    if pct != self._progress_pct:
                        self._progress_pct = pct
                        self._post(self._file_progress, pct)
                pcm_o = (outs[0] if len(outs) == 1
                         else np.concatenate(outs, axis=0))
                res = audio_io.to_float(pcm_o)

            # ---- 防啸叫（可选）
            #   这里是**离线**处理，所以能整段跑：啸叫是闭环自激，离线素材里
            #   未必有；真正需要它的是实时外放。做成开关+离线跑一遍的好处是
            #   可以先在素材上验证它"只在自激频点动手、正常说话不动"。
            #   没装陷波时 process() 是逐位直通的（自检 T5）。
            if cfg.get("_anti_howl"):
                try:
                    import anti_howl
                    sup = anti_howl.HowlSuppressor(sr)
                    self._post(self._file_progress_text, "防啸叫：扫描自激频点…")
                    y = np.asarray(res, dtype=np.float64)
                    step = 1024
                    for i0 in range(0, len(y), step):
                        y[i0:i0 + step] = sup.process(y[i0:i0 + step])
                    res = y.astype(np.float32)
                    st = sup.stats()
                    if st["notches"]:
                        self._post(self._log,
                                   "防啸叫：压了 %d 个自激频点 %s，最深 %.0f dB"
                                   % (len(st["notches"]),
                                      "、".join("%.0f Hz" % n[0] for n in st["notches"]),
                                      min(n[1] for n in st["notches"])))
                    else:
                        self._post(self._log, "防啸叫：没检测到自激频点，音轨未改动")
                except Exception as e:
                    self._post(self._log, "防啸叫失败（%r），音轨按原样输出" % e)

            # 关窗口时不再往下写盘：写一半的 WAV 比没写更糟
            if self._abort.is_set():
                self._post(self._log, "已取消：文件处理在写盘前停止")
                return
            self._post(self._file_progress_text, "正在写输出文件…")
            real_out = self._save_audio_retry(out, res, sr, "输出文件")
            if want_stems:
                self._post(self._file_progress_text, "正在写人声 / 伴奏音轨…")
            stem_paths = self._save_stems(out, stems, sr) if want_stems else []
            dur = time.time() - t0
            with self._proc_lock:
                self.proc_data = res
                self.proc_sr = sr
                self._proc_at = time.time()
            self._proc_split_info = split_info
            self._post(self._proc_done, real_out, dur, len(res), sr, stem_paths)
        except Exception as e:
            out_path = locals().get("out", "")
            self._post(self._proc_failed,
                       "%r\n输出：%s\n（若是 Permission denied：关掉占用该文件的播放器/QQ，"
                       "或换一个输出目录再试）" % (e, out_path))

    # ------------------------------------------------------------------
    def _split_worker(self, cfg, data, sr):
        """分离：左声道=人声，右声道=伴奏。返回 (立体声结果, 音轨 dict, 统计)。

        两条前端：
            计算机本地模型 -> demucs 神经网络（干净，但文件模式整段算）
            FPGA 内部模型  -> 中置提取 + 谐波梳（纯 DSP，和实时同源）

        2026-10-20 改版：不再按男女分左右，也不再"伴奏两边都留"。
        输出严格是**一个声道一条音轨**：左人声、右伴奏，两路各自可调音量。
        """
        import vocal_split as vs

        sp_cfg = dict(cfg.get("split") or {})
        swap = bool(sp_cfg.get("swap", False))
        vg = float(cfg.get("vocal_gain", 1.0))
        ag = float(cfg.get("acc_gain", 1.0))

        # ★ AI 可用性必须**在这里现查**，不能用 import 时的结论（见文件头的
        #   _ai_ready 注释）。这一步会挂载本机 torch，几秒，只在第一次发生。
        want_ai = (cfg.get("engine") == dsp.ENGINE_LOCAL_MODEL)
        ai_why = ""
        if want_ai:
            self._post(self._file_progress_text, "检查本机 AI 运行时（torch / demucs）…")
            use_ai, ai_why = _ai_ready()
        else:
            use_ai = False
        if want_ai and not use_ai:
            self._post(self._log,
                       "⚠ 选了「计算机本地模型」，但本机 AI 跑不起来（%s）。"
                       "本次按纯 DSP 中置提取处理 —— 人声里会带上所有居中乐器。"
                       % ai_why)

        if use_ai:
            ai_cfg = cfg.get("ai") or {}
            self._post(self._file_progress_text, "AI 提取人声 / 伴奏（demucs）…")
            voc, acc = ai_separate.separate(
                data, sr,
                progress=lambda s: self._post(self._file_progress_text, s),
                device=ai_cfg.get("device", "auto"),
                quality=ai_cfg.get("quality", "std"))
            # 主输出（左人声 / 右伴奏）必须折叠成单声道 —— 一个立体声文件的
            # 两条声道要各放一条音轨，这是文件格式的硬限制。
            #
            # ★ 但**导出的 stems 不能跟着折叠**。实测这首歌的伴奏 L/R 相关
            #   只有 0.73、side/mid 是 −8 dB（也就是立体声宽度相当宽），
            #   0.5*(L+R) 会把 side 分量整体丢掉：伴奏立刻变扁、发闷，
            #   人声的混响尾音也塌到中间，听起来就是"分离得不干净"。
            #   stems 是给人单独听/给下游混音的，必须是原始立体声。
            n = min(len(voc), len(acc))
            voc_st = np.asarray(voc[:n], dtype=np.float64)
            acc_st = np.asarray(acc[:n], dtype=np.float64)
            if voc_st.ndim == 1:
                voc_st = np.repeat(voc_st.reshape(-1, 1), 2, axis=1)
            if acc_st.ndim == 1:
                acc_st = np.repeat(acc_st.reshape(-1, 1), 2, axis=1)
            voc_m = 0.5 * (voc_st[:, 0] + voc_st[:, 1])
            acc_m = 0.5 * (acc_st[:, 0] + acc_st[:, 1])
            y = np.empty((n, 2), dtype=np.float64)
            v_out, a_out = (acc_m, voc_m) if swap else (voc_m, acc_m)
            y[:, 0] = v_out * (ag if swap else vg)
            y[:, 1] = a_out * (vg if swap else ag)
            v_stem, a_stem = voc_st * vg, acc_st * ag
            info = dict(engine="demucs（%s 档）+ 左人声/右伴奏"
                               % (ai_cfg.get("quality") or "std"),
                        vocal_pct=100.0,
                        mono=bool(np.max(np.abs(data[:, 0] - data[:, 1])) < 1e-6),
                        route=("人声→右 / 伴奏→左" if swap else "人声→左 / 伴奏→右"),
                        vocal_gain=vg, acc_gain=ag,
                        stems_stereo=True)
        else:
            self._post(self._file_progress_text, "中置提取：人声 / 伴奏…")
            sp = vs.VocalSplitter(sr)
            sc = dict(sp_cfg)
            sc.pop("route", None)
            sp.configure(**sc)
            self._post(self._file_progress_text, "按声道分配人声 / 伴奏…")
            y, st = sp.process(data, block=8192, stems=True,
                               progress=lambda k, tot: self._post(
                                   self._file_progress,
                                   int(100.0 * k / max(1, tot))))
            y = np.asarray(y, dtype=np.float64).copy()
            v_stem = np.asarray(st["vocal"], dtype=np.float64) * vg
            a_stem = np.asarray(st["accomp"], dtype=np.float64) * ag
            y[:, 0] *= (ag if swap else vg)
            y[:, 1] *= (vg if swap else ag)
            ss = sp.summary()
            info = dict(engine="中置提取 + 谐波梳", vocal_pct=ss.get("vocal_pct", 0.0),
                        mono=bool(ss.get("mono")),
                        route=("人声→右 / 伴奏→左" if swap else "人声→左 / 伴奏→右"),
                        vocal_gain=vg, acc_gain=ag)
            if want_ai:
                # 用户明明选了 AI 却没跑成 —— 必须写在完成提示里，不能只留一行日志，
                # 否则会拿 DSP 的结果去判断"AI 分离效果"，白白怀疑算法。
                info["engine"] = "中置提取 + 谐波梳（⚠ AI 未启用：%s）" % ai_why

        # 峰值归一化：y 与 stems 一起看。stems 现在是立体声、没经过折叠，
        # 峰值可能比主输出更高（side 分量相加的关系），只按 y 的 peak 收
        # 会让 stems 过载被裁剪。
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        for _s in (v_stem, a_stem):
            if _s is not None and np.size(_s):
                peak = max(peak, float(np.max(np.abs(_s))))
        if peak > 0.99:
            # 两路按同一比例收，保持用户调好的"人声/伴奏"响度比
            k = 0.99 / peak
            y = y * k
            v_stem = v_stem * k
            a_stem = a_stem * k
            info["peak_norm"] = peak
            self._post(self._log,
                       "分离输出峰值 %.2f，已整体归一化到 0.99（不改变人声/伴奏比例）"
                       % peak)

        stems = {"vocal": v_stem.astype(np.float32),
                 "accomp": a_stem.astype(np.float32)}
        return (y.astype(np.float32), stems, info)

    def _hq_pitch_worker(self, cfg, data, sr):
        """文件变声的高质量频域变调（不走板上的时域延迟线）。

        用离线整段的 hq_voice.process：比实时那条流式链路少了块边界的
        cross-fade 损耗，质量更好（文件模式不赶时间，就该用最好的）。
        共振峰保持强度跟着界面上的滑条走 —— 早期版本这里写死 1.0，
        滑条动了但输出没动，是个假控件。
        """
        import hq_voice

        def rep(s):
            self._post(self._file_progress_text, s)

        semi = float(cfg.get("semi_exact", 0.0))
        formant = float(cfg.get("formant", 1.0))
        r = hq_voice.semitone_to_ratio(semi)
        cols = []
        for c in range(2):
            rep("高质量变调（%s声道）…" % ("左" if c == 0 else "右"))
            cols.append(hq_voice.process(
                np.ascontiguousarray(data[:, c]).astype(np.float64), sr, r,
                formant=formant, progress=rep))
        n = min(cols[0].size, cols[1].size)
        y = np.stack([cols[0][:n], cols[1][:n]], axis=1) * float(cfg.get("gain", 1.0))
        self._post(self._log,
                   "文件变声用了高质量频域算法：音高 %+.1f 半音，共振峰保持 %d%%"
                   "（不走双抽头延迟线，所以不会有花栗鼠效应）"
                   % (semi, int(round(formant * 100))))
        return y.astype(np.float32)

    def _pick_rvc_dir(self):
        d = filedialog.askdirectory(title="选择 RVC 模型目录（放 .pth 的那一层）")
        if d:
            self.v_ai_rvc_dir.set(d)

    def _file_progress_text(self, txt):
        self.lb_file_state.configure(text="处理中… %s" % txt)

    def _pb_stop(self):
        """把进度条从"滚动中"恢复成确定式（AI 模式用过 indeterminate）。"""
        try:
            self.pb_file.stop()
            self.pb_file.configure(mode="determinate")
        except Exception:
            pass

    @staticmethod
    def _stem_path(out, tag):
        """给输出路径旁边派生一个音轨文件名：xxx_变声.wav -> xxx_变声_人声.wav"""
        root, ext = os.path.splitext(out)
        return "%s_%s%s" % (root, tag, ext or ".wav")

    def _save_audio_retry(self, path, data, sr, tag="输出"):
        """写音频；失败时重试，实在不行改存带时间戳的备选文件，**不往上抛**。

        ★ 原来只 catch OSError，而且只有 PermissionError 才算"可重试" ——
          于是下面这些全都会直接冒出去变成"输出文件报错"：
            * libsndfile 的 `sampling rate not specified`（采样率 0，是 ValueError/RuntimeError）
            * 目标目录不存在（FileNotFoundError，虽然是 OSError 但不在可重试名单里）
            * 数据里有 NaN/Inf（libsndfile 报错，同样不是 OSError）
          现在统一：先建目录、先归一化采样率，再写；任何异常都重试几次，
          最后一次换备选文件名，仍然失败才把**原因**报出来。
        """
        root, ext = os.path.splitext(str(path))
        last = None
        try:
            sr = audio_io.norm_sample_rate(sr)
        except Exception as e:
            self._post(self._log, "%s 采样率无效，已回退 48000 Hz：%s" % (tag, e))
            sr = 48000
        try:
            audio_io.ensure_dir(path)
        except Exception as e:
            self._post(self._log, "%s 目录建不出来（%r），改用临时目录" % (tag, e))
            import tempfile
            root = os.path.join(tempfile.gettempdir(),
                                os.path.basename(root) or "acx720_out")
            path = root + (ext or ".wav")

        for i in range(4):
            try:
                return audio_io.save_audio(path, data, sr)
            except Exception as e:
                last = e
                if i == 0:
                    # 第一次就把**真实异常**记下来。之前这里只留到最后一个
                    # 变量里，最后弹给用户的是一句 PermissionError，没有路径、
                    # 没有 errno 细节，等于把最有用的线索丢了。
                    self._post(self._log, "%s 第 1 次写入失败：%r（目标 %s）"
                               % (tag, e, path))
                time.sleep(0.4 * (i + 1))

        # 换备选文件名再试一次
        alt = "%s_%s%s" % (root, time.strftime("%H%M%S"), ext or ".wav")
        try:
            p = audio_io.save_audio(alt, data, sr)
            self._post(self._log, "%s 原路径写不了，改存：%s（原因 %r）"
                       % (tag, os.path.basename(alt), last))
            return p
        except Exception as e:
            last2 = e
            self._post(self._log, "%s 换名也不行（%r），再换目录" % (tag, last2))

        # ★ 最后一道：整个目录都写不进去时换到别的目录。
        #   实测有环境对某个目录（比如 Downloads）的系统性拒绝写，换名没用，
        #   但同一个用户的家目录底下别的目录是能写的。宁可多存一处，
        #   也别让用户拿到一句"处理失败"却什么都没有。
        import tempfile
        for d in (os.path.join(os.path.expanduser("~"), "Music", "ACX720变声"),
                  os.path.join(tempfile.gettempdir(), "ACX720VoiceChanger"),
                  os.path.join(_app_dir(), "输出")):
            try:
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, os.path.basename(root) +
                                 time.strftime("_%H%M%S") + (ext or ".wav"))
                res = audio_io.save_audio(p, data, sr)
                self._post(self._log, "%s 目标目录不可写，已改存到这里：%s"
                           % (tag, res))
                return res
            except Exception as e:
                last = e
                continue
        self._post(self._log, "%s 彻底写失败：%r" % (tag, last))
        raise last

    def _save_stems(self, out, stems, sr):
        """把 人声 / 伴奏 两条音轨写到输出文件旁边，返回实际路径列表。

        stems 只有 "vocal" / "accomp" 两个键（2026-10-21 起男/女轨已删除）。
        """
        pairs = (("人声", "vocal"), ("伴奏", "accomp"))
        saved = []
        for tag, key in pairs:
            a = stems.get(key)
            if a is None or a.size == 0:
                continue
            try:
                p = self._save_audio_retry(self._stem_path(out, tag), a, sr, tag)
                saved.append(p)
            except Exception as e:
                self._post(self._log, "音轨 %s 写出失败：%r" % (tag, e))
        return saved

    def _file_progress(self, pct):
        self.pb_file["value"] = pct
        self.lb_file_state.configure(text="处理中… %d%%" % pct)

    def _proc_done(self, out, dur, n, sr, stem_paths=()):
        self._processing = False
        self._pb_stop()
        self.pb_file["value"] = 100
        self._show_file_spectrum(self.proc_data, self.proc_sr, "out")
        extra = ""
        info = getattr(self, "_proc_split_info", None)
        if info:
            extra = ("\n分离：%s（%s）\n  人声帧 %.1f%%   人声音量 %.0f%% / 伴奏音量 %.0f%%"
                     "%s"
                     % (info.get("engine", ""), info.get("route", ""),
                        info.get("vocal_pct", 0.0),
                        info.get("vocal_gain", 1.0) * 100.0,
                        info.get("acc_gain", 1.0) * 100.0,
                        "  ⚠ 单声道源，已降级为谐波提取" if info.get("mono") else ""))
            self._log("分离（%s）：%s，人声帧 %.1f%%"
                      % (info.get("engine", ""), info.get("route", ""),
                         info.get("vocal_pct", 0.0)))
        if stem_paths:
            extra += "\n另存音轨：" + "、".join(os.path.basename(x) for x in stem_paths)
            extra += "\n  ▲ 判断分离质量请听「…_人声.wav」，别拿主输出文件判断" \
                     "（它天然是左人声 / 右伴奏，两条声道会同时响）"
            self._log("人声 / 伴奏音轨已写出：%s"
                      % "，".join(os.path.basename(x) for x in stem_paths))
        elif info:
            # 没导出音轨时，至少把"主输出是什么"讲清楚，别让人误判
            extra += "\n提示：主输出是「左人声 / 右伴奏」双轨文件；" \
                     "想单独听人声请勾上「同时导出人声 / 伴奏音轨」"
            self._log("未导出单独音轨：主输出 %s 的两条声道分别是人声和伴奏"
                      % os.path.basename(out))
        self.lb_file_state.configure(
            text="完成：%s  用时 %.2f 秒（%.0f 倍实时）\n%s%s"
                 % (os.path.basename(out), dur,
                    (n / float(sr)) / dur if dur > 0 else 0,
                    audio_io.duration_str(n, sr), extra))
        self.btn_proc.configure(state="normal")
        # 结果直接装进内嵌播放器，点 ▶ 就能听；左右声道开关立刻可用
        if self.proc_data is not None:
            self.player.load(self.proc_data, self.proc_sr, "变声结果")
            self._play_data = self.player.data
            self.player_bar.refresh()
        self._log("处理完成 -> %s  用时 %.2fs（结果缓存 %.1f MB，闲置 %d 分钟后自动释放）"
                  % (out, dur, self._mb(np.asarray(self.proc_data).nbytes),
                     PROC_TTL_SEC // 60))
        self._update_cache_label()

    def _proc_failed(self, msg):
        self._processing = False
        self._pb_stop()
        self.lb_file_state.configure(text="处理失败：%s" % msg)
        self.btn_proc.configure(state="normal")
        self._log("处理失败：%s" % msg)
        messagebox.showerror("处理失败", msg)

    def _unload_source(self):
        """手动卸载素材（自动卸载要等闲置 30 分钟，这里是立刻放掉）。"""
        if self.file_data is None:
            self._log("当前没有已载入的素材")
            return
        if self._processing:
            messagebox.showinfo("正在处理", "处理还没结束，稍后再卸载。")
            return
        self._stop_play()
        self._release_source("手动卸载", manual=True)
        self._update_cache_label()

    def _player_log(self, msg):
        """Player 在构造早期就可能回调；日志框没建好时静默丢弃。"""
        if hasattr(self, "log"):
            self._log(msg)

    def _play_result(self):
        if self.proc_data is None:
            return
        self._proc_at = time.time()        # 播放视为"使用过"，续上 TTL
        self._play(self.proc_data, self.proc_sr, "变声结果")

    def _play_source(self):
        """把原声载入内嵌播放器并开始播放（不再限制 15 秒）。"""
        if self.file_data is None:
            return
        self._file_at = time.time()
        self._play(audio_io.to_stereo(self.file_data), self.file_sr, "原声")

    def _play(self, data, sr, tag, autoplay=True):
        if data is None:
            return
        if not HAVE_SD:
            messagebox.showerror("缺少依赖", "没有 sounddevice，无法播放。")
            return
        self.player.load(data, sr, tag)
        # 播放器持有的是同一份数组；用 _play_data 让缓存释放逻辑知道
        # "这份数据正在被播放器使用"，不要偷偷释放掉。
        self._play_data = self.player.data
        self.player_bar.refresh()
        if autoplay and not self.player.play():
            self._log("播放失败：请检查默认输出设备是否可用")
        self.playing = self.player.playing

    def _stop_play(self):
        self.player.stop()
        self.playing = False
        # 播放器仍然持有这段数据（用户还能再点播放），缓存巡检要知道它还在用
        self._play_data = self.player.data
        try:
            self.player_bar.tick()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _log(self, msg):
        # 先进磁盘日志：界面日志框可能还没建好（构造期就会调用），
        # 也可能被用户滚走了；磁盘那份才是排障用的。
        file_log(msg)
        if not hasattr(self, "log"):
            return
        ts = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", "[%s] %s\n" % (ts, msg))
        self.log.see("end")
        self.log.configure(state="disabled")
        self._log_lines += 1
        if self._log_lines > LOG_TRIM_AT:
            self._trim_log()

    # ------------------------------------------------------------------
    def _post(self, fn, *args):
        """工作线程 -> 主线程的线程安全投递。队列满时丢最旧的。"""
        try:
            self._ui_q.put_nowait((fn, args))
        except queue.Full:
            try:
                self._ui_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._ui_q.put_nowait((fn, args))
            except queue.Full:
                pass

    def _drain_ui(self):
        for _ in range(80):
            try:
                fn, args = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as e:
                try:
                    self._log("界面回调失败：%r" % (e,))
                except Exception:
                    pass

    def _start_poll(self):
        self._poll()
        self.after(50, self._start_poll)

    def _cur_tab(self):
        try:
            return self.nb.index(self.nb.select())
        except Exception:
            return 0

    def _wave_pair(self, raw):
        """四路波形按「显示」下拉做取舍，返回 [in_l, in_r, out_l, out_r]。

        默认「同屏四路」原样返回；「只看左/右」把另一侧置 None（第四路
        会显示"无信号"而不是悄悄消失）；「平均」把每一对 L/R 合成一路
        放在左泳道，右泳道留空。DSP 永远四路独立算，这里只影响画图。
        """
        m = self.v_spec_mode.get()
        il, ir, ol, orr = raw
        if m == "只看左":
            return [il, None, ol, None]
        if m == "只看右":
            return [None, ir, None, orr]
        if m == "平均":
            def _avg(a, b):
                if a is None:
                    return b
                if b is None:
                    return a
                n = min(a.size, b.size)
                return 0.5 * (a[-n:] + b[-n:])
            return [_avg(il, ir), None, _avg(ol, orr), None]
        return [il, ir, ol, orr]

    def _spec_pair(self, l, r):
        """按「显示」选择返回要画的两路频谱 (左曲线, 右曲线)。

        默认「左右同时」直接返回两路，画成并排双色柱；其余选项只画一路
        （满格）。注意这只是**显示**上的取舍，DSP 计算永远左右分开做。
        """
        m = self.v_spec_mode.get()
        if m == "只看左":
            return l, None
        if m == "只看右":
            return None, r
        if m == "平均":
            if l is None:
                return r, None
            if r is None:
                return l, None
            return 0.5 * (np.asarray(l, dtype=np.float32)
                          + np.asarray(r, dtype=np.float32)), None
        return l, r

    def _spec_curve(self, x, sr):
        """单个声道的一段样本 -> 72 根柱的平均谱（文件模式用）。"""
        try:
            return specmod.spectrum_of_signal(np.asarray(x, dtype=np.float32).reshape(-1),
                                              sr)
        except Exception:
            return None

    def _poll(self):
        # 参数快照在主线程刷新：音频线程只读快照，不碰 Tk 变量。
        self._refresh_cfg()
        # 把当前模式同步给外接 OLED（声卡桥会把它转成串口配置包）
        self._publish_mode()
        # 先执行工作线程投递过来的界面更新（进度、完成、失败）
        self._drain_ui()
        # 声卡桥状态：每 BRIDGE_POLL_SEC 秒查一次（poll 本身是 50 ms 一跳）
        now = time.time()
        if now - self._bridge_t >= BRIDGE_POLL_SEC:
            self._bridge_t = now
            self._bridge_refresh()
        # 播放器：更新进度/时间/按钮文字，并把 playing 同步给缓存巡检
        try:
            self.player_bar.tick()
            self.playing = self.player.playing
        except Exception:
            pass
        rt = self.rt
        if rt.running:
            lv = min(1.0, rt.level * 6.0)
            self.pb_level["value"] = lv * 100.0
            self.lb_rt_stat.configure(
                text="缓冲 %d 块 | 录音溢出 %d | 播放欠载 %d | 输入削波 L%d/R%d "
                     "| 输出削波 L%d/R%d%s"
                     % (rt.load(), rt.overflow, rt.underrun,
                        rt.clip_in[0], rt.clip_in[1],
                        rt.clip_out[0], rt.clip_out[1],
                        ("  !! %s" % rt.error) if rt.error else ""))
            # 只有停在实时页时才刷右上图，否则会盖掉文件页的静态图
            if self.v_spec_on.get() and self._cur_tab() == 0:
                self.spec.set_sr(rt.sr_out)
                self.scope.set_sr(rt.sr_out)
                raw = [rt.ring_in[0].latest(), rt.ring_in[1].latest(),
                       rt.ring_out[0].latest(), rt.ring_out[1].latest()]
                raw = self._wave_pair(raw)
                if self.v_view.get() == "四路波形":
                    self.scope.set_in(raw[0], raw[1])
                    self.scope.set_out(raw[2], raw[3])
                    self.scope.draw()
                else:
                    il, ir = self._spec_pair(
                        specmod.compute_spectrum(raw[0], rt.sr_out),
                        specmod.compute_spectrum(raw[1], rt.sr_out))
                    ol, orr = self._spec_pair(
                        specmod.compute_spectrum(raw[2], rt.sr_out),
                        specmod.compute_spectrum(raw[3], rt.sr_out))
                    self.spec.set_in(il, ir)
                    self.spec.set_out(ol, orr)
                    self.spec.draw()
        else:
            self.pb_level["value"] = 0
        self._update_leds()

    # ------------------------------------------------------------------
    def _update_leds(self):
        """把所有能判定的检查项刷到灯板上，并汇总出一条建议文字。"""
        L = self.leds
        rt = self.rt
        adv = []

        def dbfs(p):
            return 20.0 * math.log10(p) if p > 1e-6 else -90.0

        idev, odev = self._rt_dev_indices()
        if not HAVE_SD:
            L.set("dev_in", ERR, "缺 sounddevice")
            L.set("dev_out", ERR, "缺 sounddevice")
            adv.append("没有 sounddevice，装一下：pip install sounddevice")
        else:
            L.set("dev_in", OK if idev is not None else ERR,
                  "已选" if idev is not None else "无输入设备")
            L.set("dev_out", OK if odev is not None else ERR,
                  "已选" if odev is not None else "无输出设备")
            if idev is None:
                adv.append("没找到输入设备，插上麦克风后点「刷新设备列表」")
            if odev is None:
                adv.append("没找到输出设备，检查耳机/扬声器")

        sr = 48000
        try:
            sr = int(self.cb_sr.get())
        except Exception:
            pass
        if rt.running:
            L.set("sr", OK if rt.sr_in == rt.sr_out else WARN,
                  "%d Hz" % rt.sr_out)
        else:
            L.set("sr", IDLE, "%d Hz 待启动" % sr)

        if rt.running:
            L.set("engine", OK, "运行中")
        elif rt.error:
            L.set("engine", ERR, "已停止（异常）")
        else:
            L.set("engine", IDLE, "已停止")

        if rt.running:
            p = max(rt.peak_in)
            if p > 0.02:
                L.set("sig_in", OK, "L %.0f / R %.0f dBFS"
                      % (dbfs(rt.peak_in[0]), dbfs(rt.peak_in[1])))
            elif p > 0.001:
                L.set("sig_in", WARN, "偏低 %.0f dBFS" % dbfs(p))
                adv.append("输入电平偏低：调大「输入增益」，或换个麦克风")
            else:
                L.set("sig_in", WARN, "静音")
                adv.append("输入是静音：确认选对了麦克风，且系统没把它静音")
            q = max(rt.peak_out)
            L.set("sig_out", OK if q > 0.001 else WARN,
                  "L %.0f / R %.0f dBFS" % (dbfs(rt.peak_out[0]), dbfs(rt.peak_out[1]))
                  if q > 0.001 else "无输出")
            if q <= 0.001:
                adv.append("输出无声：检查输出设备与系统音量")
        else:
            L.set("sig_in", IDLE, "")
            L.set("sig_out", IDLE, "")

        # 削波按声道分别判定：只有一侧过载时，混成单声道看是看不出来的。
        # 数字是**近期**计数（每秒自动衰减），所以很久以前的一次过载不会
        # 把灯一直钉死在红色上；累计值在日志里定期报告。
        for tag, arr, fix in (("in", rt.clip_in, "调小「输入增益」"),
                              ("out", rt.clip_out, "调小「输出音量」")):
            for ci, side in ((0, "l"), (1, "r")):
                key = "clip_%s_%s" % (tag, side)
                n = int(arr[ci])
                if n == 0:
                    L.set(key, OK if rt.running else IDLE,
                          "正常" if rt.running else "")
                elif n < 20:
                    L.set(key, WARN, "偶发×%d" % n)
                else:
                    L.set(key, ERR, "持续×%d" % n)
                    adv.append("%s 声道%s近期已削波 %d 次：%s"
                               % ("输入" if tag == "in" else "输出",
                                  "L" if side == "l" else "R", n, fix))
        if rt.running:
            d = abs(rt.peak_out[0] - rt.peak_out[1])
            if d > 0.02 and (rt.peak_out[0] > 0.005 or rt.peak_out[1] > 0.005):
                if d > 0.5 * max(rt.peak_out):
                    adv.append("左右输出电平差 %.0f dB —— 分离模式下属正常，"
                               "其他模式请检查声像设置" % abs(dbfs(rt.peak_out[0])
                                                              - dbfs(rt.peak_out[1])))

        load = rt.load()
        # ★ 缓冲灯按"最近 10 秒"的溢出/欠载增量判定。以前用累计值，启动后
        #   历史上抖过 20 次灯就永远 ERR"卡顿"，看起来像缓存/缓冲一直异常。
        #   改成滚动窗口后，偶发抖动 10 秒内自动回绿；累计值仍写日志可查。
        now_s = time.time()
        self._buf_hist.append((now_s, rt.overflow, rt.underrun))
        while self._buf_hist and (now_s - self._buf_hist[0][0]) > 10.0:
            self._buf_hist.pop(0)
        _t0, _o0, _u0 = self._buf_hist[0]
        bad = (rt.overflow - _o0) + (rt.underrun - _u0)
        if not rt.running:
            L.set("buf", IDLE, "")
        elif bad == 0 and load < 100:
            L.set("buf", OK, "稳定 %d 块" % load)
        elif bad < 5:
            L.set("buf", WARN, "抖动 近10s 溢%d 欠%d" % (rt.overflow - _o0,
                                                        rt.underrun - _u0))
            adv.append("有轻微丢帧（近 10 秒 %d 次）：把「缓冲块大小」调大到 2048"
                       % bad)
        else:
            L.set("buf", ERR, "卡顿 近10s 溢%d 欠%d" % (rt.overflow - _o0,
                                                       rt.underrun - _u0))
            adv.append("近 10 秒丢帧 %d 次：增大块大小，或关掉占用声卡的其他程序"
                       % bad)

        if rt.error:
            L.set("dsp", ERR, "内核异常")
            adv.append("DSP 内核报错：%s" % rt.error)
        else:
            L.set("dsp", OK, "定点内核就绪")

        # ---- 本地 AI 环境 ----
        # probe() 会缓存，但第一次要 import torch（几秒），所以放在启动时的
        # 后台线程里预热；没预热完之前不在这里调，免得卡住 50 ms 的轮询。
        if local_ai is None:
            L.set("ai", ERR, "模块未加载")
        elif not self._ai_probed:
            L.set("ai", BUSY, "自检中…")
        else:
            ok, why = local_ai.demucs_ready()
            env = local_ai.probe()
            # 「分离 × 计算机本地模型」才真的会去调 demucs，其余组合这盏灯
            # 只是告诉用户"环境是好的，但你这次没用它"
            if self.v_module.get() != dsp.MODULE_SPLIT or \
                    self.v_engine.get() != dsp.ENGINE_LOCAL_MODEL:
                L.set("ai", IDLE, ("可用，未启用" if ok else "缺依赖"))
            elif ok:
                L.set("ai", OK, "%s / CUDA%s"
                      % (local_ai.DEMUCS_MODEL, "可用" if env["cuda"][0] else "不可用"))
                if not env["cuda"][0]:
                    adv.append("没有可用的 CUDA，本地 AI 会退回 CPU，慢 5~15 倍")
            else:
                L.set("ai", WARN, "不可用")
                adv.append("本地 AI 不可用（%s）：装 pip install demucs 后重启本程序" % why)

        if self.file_data is None:
            L.set("file", IDLE, "未载入")
        elif self._processing:
            L.set("file", BUSY, "处理中")
        elif self.proc_data is not None:
            L.set("file", OK, "已输出")
        else:
            L.set("file", OK, "已载入待处理")

        # 缓存：把"长时间运行会不会涨爆"这件事变成一个看得见的灯。
        # 超过 CACHE_AUTO_MB 巡检会自动清理（结果→素材→播放缓冲），
        # 所以黄灯=自动清理暂时压不住（正在处理/都在使用中），会自己回落。
        cb = self._cache_bytes()
        mb = self._mb(cb)
        if cb <= 0:
            L.set("cache", IDLE, "无缓存")
        elif mb > CACHE_WARN_MB:
            L.set("cache", WARN, "%.0f MB" % mb)
            if self._processing:
                adv.append("缓存 %.0f MB：正在处理，处理完会自动清理较早缓存"
                           % mb)
            else:
                adv.append("缓存 %.0f MB：下次巡检将自动清理较早缓存；"
                           "仍偏高请换更短的素材" % mb)
        else:
            L.set("cache", OK, "%.1f MB / 已清理 %d 次" % (mb, self._hk_cleaned))

        # 板上档位：拨码只有 16 档，半音是连续的，这里给出映射误差。
        # 只有「变调 × FPGA 直算」能一对一招上板子，其余组合板上没有。
        if self.v_module.get() != dsp.MODULE_PITCH or \
                self.v_engine.get() != dsp.ENGINE_FPGA_DIRECT:
            # 年龄变声不是"板上没有"——它是**不靠拨码**，参数从串口下发，
            # 所以这里要分开说，否则会被误读成"这个模式板子不支持"。
            if (self.v_module.get() == dsp.MODULE_PITCH and
                    self.v_engine.get() == dsp.ENGINE_AGE):
                L.set("sw", IDLE, "由年龄参数下发（不走拨码）")
            else:
                L.set("sw", IDLE, "板上无此组合")
        else:
            semi = self.v_semi.get()
            dp = dsp.semitone_to_dphase(semi)
            v = dsp.dphase_to_switch(dp)
            v_raw = int(round(float(dp) / 6.0)) + 5
            semi_act = dsp.ratio_to_semitone(dsp.dphase_to_ratio(dsp.switch_to_dphase(v)))
            err = abs(semi - semi_act)
            if v_raw < 0 or v_raw > 15:
                L.set("sw", WARN, "超出板范围 sw=%d" % max(0, min(15, v)))
                adv.append("±%.1f 半音超出板上 16 档范围，板子只能取最近档（sw[5:2]=%d）"
                           % (semi, max(0, min(15, v))))
            elif err < 0.08:
                L.set("sw", OK, "吻合 sw=%d" % v)
            else:
                L.set("sw", WARN, "偏差 %.2f 半音" % err)

        L.draw()
        txt = ("建议：" + "；".join(adv)) if adv else ""
        if txt != self._last_advice:
            self._last_advice = txt
            self.lb_advice.configure(text=txt)

    # ------------------------------------------------------------------
    def _on_close(self):
        # 文件处理线程是 daemon：直接 destroy 会让它被硬杀在写盘中间，
        # 留下一个截断的音频文件。先置中止标志并等它自己收尾。
        self._abort.set()
        t = getattr(self, "_proc_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        try:
            self.rt.stop()
        except Exception:
            pass
        self._stop_play()
        try:
            self.player.close()
        except Exception:
            pass
        self.destroy()


def _selftest(out_path="_selftest.txt"):
    """无窗口自检：给打包后的 EXE 用（ACX720VoiceChanger.exe --selftest）。

    会真的建一次 GUI、跑几轮刷新、喂一段正弦看频谱峰落在哪里，
    结果写进 _selftest.txt。EXE 是 windowed 的，看不到 stdout，
    只能靠这个文件确认内部依赖（numpy / sounddevice / 效果链）都活着。
    """
    lines = []
    ok = True

    def rec(name, good, detail=""):
        nonlocal ok
        if not good:
            ok = False
        lines.append("[%s] %s %s" % ("PASS" if good else "FAIL", name, detail))

    def _gz(x, f, sr):
        """单点 DFT 幅度：比 72 根对数频谱柱精确得多，适合做数值判据。"""
        x = np.asarray(x, dtype=np.float64)
        k = np.arange(x.size)
        return float(abs(np.sum(x * np.exp(-1j * 2 * np.pi * f / sr * k)))
                     * 2.0 / max(1, x.size))

    def _peak_hz(x, sr):
        """加窗 FFT 的谱峰频率。"""
        x = np.asarray(x, dtype=np.float64)
        x = x - x.mean()
        sp = np.abs(np.fft.rfft(x * np.hanning(x.size)))
        f = np.fft.rfftfreq(x.size, 1.0 / sr)
        return float(f[int(np.argmax(sp))])

    try:
        import numpy
        rec("numpy", True, numpy.__version__)
    except Exception as e:
        rec("numpy", False, repr(e))
    try:
        import sounddevice
        rec("sounddevice", True, sounddevice.__version__)
    except Exception as e:
        rec("sounddevice", False, repr(e))
    try:
        import tkinter
        rec("tkinter", True, "Tk %s" % tkinter.TkVersion)
    except Exception as e:
        rec("tkinter", False, repr(e))

    # ★ 这两个模块是在**函数内部** import 的（防啸叫只在文件处理里用、
    #   年龄包只在点"发送到 FPGA"时用），打包时很容易被漏掉；漏了的表现
    #   不是报错，而是该功能静默失效（"防啸叫失败 No module named ..."）。
    #   所以自检里必须真的 import 一次，并且跑一遍它们自己的判据。
    try:
        import anti_howl
        sup = anti_howl.HowlSuppressor(24000)
        _t = np.arange(2400) / 24000.0
        _y = sup.process(0.2 * np.sin(2 * np.pi * 200.0 * _t))
        _bit = bool(np.max(np.abs(_y - 0.2 * np.sin(2 * np.pi * 200.0 * _t))) == 0.0)
        rec("防啸叫模块（含逐位直通）", _bit,
            "陷波=%d 深=%.0f dB" % (len(sup.notches), sup.duck_db))
    except Exception as e:
        rec("防啸叫模块", False, "%s: %s" % (type(e).__name__, e))
    try:
        import age_link
        _pk, _p = age_link.packet_from_ages(30, 70, "m")
        rec("年龄参数包（含 CRC）", len(_pk) == 16 and _pk[:2] == b"\xAA\x55",
            "r=%.3f beta=%.3f %d 字节" % (_p["r"], _p["beta"], len(_pk)))
    except Exception as e:
        rec("年龄参数包", False, "%s: %s" % (type(e).__name__, e))

    # 频谱频率刻度：1 kHz 正弦的峰应落在 1 kHz 那一格
    try:
        sr = 48000
        t = np.arange(8192, dtype=np.float32) / float(sr)
        x = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        db = specmod.compute_spectrum(x, sr)
        i = int(np.argmax(db))
        f0 = specmod.F_MIN * (sr * 0.5 / specmod.F_MIN) ** (i / float(specmod.NBAR))
        rec("频谱刻度", 850.0 < f0 < 1180.0, "1 kHz 峰落在 %.0f Hz 格" % f0)
    except Exception as e:
        rec("频谱刻度", False, repr(e))

    # 效果链仍然对：+12 半音 -> 基频翻倍
    try:
        sr = 48000
        n = 16384
        t = np.arange(n, dtype=np.float32) / float(sr)
        x = (0.6 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        c = dsp.VoiceChain()
        c.configure(mode=dsp.MODE_PITCH, dphase=dsp.semitone_to_dphase(12.0))
        y = c.process(audio_io.to_int16(x), block=4096)
        yf = audio_io.to_float(y)[n // 2:]
        db = specmod.compute_spectrum(yf, sr)
        i = int(np.argmax(db))
        f1 = specmod.F_MIN * (sr * 0.5 / specmod.F_MIN) ** (i / float(specmod.NBAR))
        rec("变调链", 380.0 < f1 < 520.0, "220 Hz +12 半音 -> %.0f Hz" % f1)
    except Exception as e:
        rec("变调链", False, repr(e))

    # 双声道分别运算：左右给不同频率，处理完各自还留在原来的声道
    try:
        sr = 48000
        n = 32768
        t = np.arange(n, dtype=np.float32) / float(sr)
        a = (0.5 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
        b = (0.5 * np.sin(2 * np.pi * 900 * t)).astype(np.float32)
        st = audio_io.to_int16(np.stack([a, b], axis=1))
        sc = dsp.StereoChain(sr)
        sc.reset()
        sc.configure(mode=dsp.MODE_PITCH, dphase=dsp.semitone_to_dphase(12.0),
                     gain=1.0)
        yo = audio_io.to_float(sc.process(st, block=4096))
        # 峰频要看实际值：时域变调有约 2% 的系统性偏低（分数延迟插值的
        # 低通效应），用固定频率做单点 DFT 会因为相位漂移而相互抵消。
        seg = slice(8192, 16384)
        l_pk = _peak_hz(yo[seg, 0], sr)
        r_pk = _peak_hz(yo[seg, 1], sr)
        l_ref = _gz(yo[seg, 0], l_pk, sr)
        r_ref = _gz(yo[seg, 1], r_pk, sr)
        cross_l = _gz(yo[seg, 0], r_pk, sr)      # 右声道的频率串进左声道了吗
        cross_r = _gz(yo[seg, 1], l_pk, sr)
        good = (500.0 < l_pk < 700.0 and 1500.0 < r_pk < 2100.0
                and cross_l < 0.2 * max(l_ref, 1e-9)
                and cross_r < 0.2 * max(r_ref, 1e-9))
        rec("双声道独立运算", good,
            "L 300Hz→%.0fHz，R 900Hz→%.0fHz，串扰 %.3f/%.3f"
            % (l_pk, r_pk, cross_l, cross_r))
    except Exception as e:
        rec("双声道独立运算", False, repr(e))

    # 分离：左声道=人声、右声道=伴奏
    # 合成一段"人声居中 + 伴奏硬左/硬右"的混音，检查两路有没有真的分开。
    try:
        import vocal_split as vs
        sr = 48000
        half = sr // 2
        n = half * 2
        t = np.arange(n, dtype=np.float64) / float(sr)
        voc = 0.30 * np.sin(2 * np.pi * 440.0 * t)          # 人声：居中
        acc_l = 0.22 * np.sin(2 * np.pi * 3000.0 * t)       # 伴奏：硬左
        acc_r = 0.22 * np.sin(2 * np.pi * 6000.0 * t)       # 伴奏：硬右
        x = np.stack([voc + acc_l, voc + acc_r], axis=1).astype(np.float32)

        sp = vs.VocalSplitter(sr)
        sp.configure(depth=1.0, broadband=0.25, harmonic=True)
        y, st = sp.process(x, block=8192, stems=True)

        v_keep = _gz(y[:, 0], 440.0, sr)
        v_leak = _gz(y[:, 1], 440.0, sr)
        a_r_keep = _gz(y[:, 1], 6000.0, sr)
        a_l_keep = _gz(y[:, 0], 3000.0, sr)
        good = (v_keep > 0.10 and a_r_keep > 0.10
                and v_leak < 0.35 * max(v_keep, 1e-9)
                and a_l_keep < 0.35 * max(a_r_keep, 1e-9)
                and set(st.keys()) == {"vocal", "accomp"})
        rec("分离：左人声 / 右伴奏", good,
            "人声留L %.3f/漏到R %.3f，伴奏留R %.3f/漏到L %.3f，音轨=%s"
            % (v_keep, v_leak, a_r_keep, a_l_keep, sorted(st.keys())))

        # 流式类（实时模式用）只要不崩、输出有限即可
        ys = sp.process(x, block=8192)
        rec("分离流式内核可运行", ys.shape == x.shape and np.isfinite(ys).all(),
            "shape=%s" % (ys.shape,))
    except Exception as e:
        rec("分离：左人声 / 右伴奏", False, repr(e))

    # 频谱同时画左右两个声道
    try:
        sr = 48000
        n = 16384
        t = np.arange(n, dtype=np.float32) / float(sr)
        ldb = specmod.compute_spectrum((0.5 * np.sin(2 * np.pi * 1000 * t)), sr)
        rdb = specmod.compute_spectrum((0.5 * np.sin(2 * np.pi * 3000 * t)), sr)
        sp = specmod.DualSpectrum.__new__(specmod.DualSpectrum)   # 不建窗口
        sp.db = {"in": [None, None], "out": [None, None]}
        sp.peak = {"in": [np.full(specmod.NBAR, specmod.DB_MIN, dtype=np.float32)
                          for _ in range(2)],
                   "out": [np.full(specmod.NBAR, specmod.DB_MIN, dtype=np.float32)
                           for _ in range(2)]}
        sp.set_in(ldb, rdb)
        both = (sp.db["in"][0] is not None and sp.db["in"][1] is not None)
        right_kept = np.array_equal(sp.db["in"][1], rdb)
        sp.set_out(ldb)                      # 单路应画满格
        single = (sp.db["out"][0] is not None and sp.db["out"][1] is None)
        rec("频谱左右同时显示", both and right_kept and single,
            "双路 %s / 单路 %s" % (both, single))
    except Exception as e:
        rec("频谱左右同时显示", False, repr(e))

    # 内嵌播放器：装载、左右声道开关、跳转（不走真实声卡）
    try:
        import player as pl
        sr = 48000
        t = np.arange(sr, dtype=np.float32) / float(sr)
        x = np.stack([0.5 * np.sin(2 * np.pi * 440 * t),
                      0.25 * np.sin(2 * np.pi * 880 * t)], axis=1)
        p = pl.Player()
        p.load(x, sr, "selftest")
        p.set_channel(0, False)
        p.set_channel(1, True)
        out = np.zeros((1024, 2), dtype=np.float32)
        p._cb(out, 1024, None, None)
        l_off = float(np.abs(out[:, 0]).max())
        r_on = float(np.abs(out[:, 1]).max())
        p.seek(0.5)
        p.set_channel(0, True)
        out2 = np.zeros((1024, 2), dtype=np.float32)
        p._cb(out2, 1024, None, None)
        l_on = float(np.abs(out2[:, 0]).max())
        good = (l_off < 1e-6 and r_on > 0.1 and l_on > 0.1
                and abs(p.position - 0.5) < 0.05)
        rec("内嵌播放器（装载/声道开关/跳转）", good,
            "关左后 L=%.3f R=%.3f，跳转 %.2fs，开左后 L=%.3f"
            % (l_off, r_on, p.position, l_on))

        # 输出设备：内嵌播放器能选声卡（试听时送到 CABLE Input 走 FPGA→音响）。
        # 用假 sd 拦下 OutputStream 的实参，不碰真实声卡。
        cap = {}

        class _FS(object):
            active = False

            def __init__(self, **kw):
                cap.clear()
                cap.update(kw)

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def close(self):
                self.active = False

        class _FSD(object):
            @staticmethod
            def OutputStream(**kw):
                return _FS(**kw)

        real_sd = pl.sd
        try:
            pl.sd = _FSD
            pd = pl.Player()
            pd.load(x, sr, "dev")
            pd.set_device(6)
            pd.play()
            dev_ok = (cap.get("device") == 6)
            pd.set_device(None)
            pd.play()
            dev_ok = dev_ok and (cap.get("device") is None)
            pd.close()
        finally:
            pl.sd = real_sd
        rec("播放器可选输出设备", dev_ok, "device 参数透传到 OutputStream")
        p.close()
    except Exception as e:
        rec("内嵌播放器（装载/声道开关/跳转）", False, repr(e))

    # 音频格式后端：写一个 FLAC 再读回来。
    # 打包成 EXE 之后最容易在这里断（PyInstaller 默认不带 ffmpeg / libsndfile），
    # 所以专门留一项自检盯着它。
    try:
        import shutil
        import tempfile

        bk = dict(audio_io.backends())
        rec("解码后端可用", bool(bk.get("soundfile") or bk.get("av")),
            "soundfile=%s av=%s librosa=%s"
            % (bk.get("soundfile"), bk.get("av"), bk.get("librosa")))

        tmp = tempfile.mkdtemp(prefix="acx_fmt_")
        try:
            fs = 48000
            t = np.arange(fs, dtype=np.float32) / float(fs)
            x = np.stack([0.4 * np.sin(2 * np.pi * 440 * t),
                          0.4 * np.sin(2 * np.pi * 660 * t)],
                         axis=1).astype(np.float32)
            wav = os.path.join(tmp, "a.wav")
            audio_io.save_wav(wav, x, fs)
            aw, sw = audio_io.load_audio(wav, {})
            rec("WAV 往返", aw.shape == x.shape and sw == fs, "%s" % (aw.shape,))

            flac = os.path.join(tmp, "a.flac")
            audio_io.save_audio(flac, x, fs)
            info = {}
            af, sf2 = audio_io.load_audio(flac, info)
            err = float(np.abs(af[:fs] - x).max())
            rec("FLAC 无损往返", af.shape[0] >= fs and sf2 == fs and err < 1e-3,
                "后端=%s 误差=%.6f" % (info.get("backend"), err))

            # PyAV 这条路曾经因为 API 变更整体失效（to_ndarray(format=...) 被删），
            # 打包时又最容易漏掉 ffmpeg 的 DLL，所以单独走一遍编码+解码。
            if bk.get("av"):
                import av

                m4a = os.path.join(tmp, "a.m4a")
                with av.open(wav) as inp, av.open(m4a, "w", format="mp4") as ost:
                    ist = inp.streams.audio[0]
                    ost_a = ost.add_stream("aac", rate=ist.codec_context.sample_rate)
                    for fr in inp.decode(audio=0):
                        for pk in ost_a.encode(fr):
                            ost.mux(pk)
                    for pk in ost_a.encode(None):
                        ost.mux(pk)
                info2 = {}
                am, sm = audio_io.load_audio(m4a, info2)
                rec("M4A/AAC 解码（PyAV）", sm == fs and am.shape[0] >= fs,
                    "后端=%s 长度=%d/%d" % (info2.get("backend"), am.shape[0], fs))
            else:
                rec("M4A/AAC 解码（PyAV）", False, "没有 av 模块")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        rec("FLAC 无损往返", False, repr(e))

    # 缓存巡检：日志裁行 + 闲置结果缓存自动释放
    try:
        app = App()
        app.update_idletasks()
        app.rt.ring_in[0].push(np.zeros(4096, dtype=np.float32))
        app.rt.ring_in[1].push(np.zeros(4096, dtype=np.float32))
        app._poll()
        app._show_file_spectrum(np.zeros((48000, 2), dtype=np.float32), 48000, "in")
        app.update_idletasks()
        n_led = len(app.leds.state)

        # 日志上限
        for i in range(LOG_MAX_LINES + 250):
            app._log("填充日志行 %d" % i)
        app._trim_log(force=True)
        nlines = int(app.log.index("end-1c").split(".")[0])
        rec("日志自动裁行", nlines <= LOG_MAX_LINES,
            "%d 行（上限 %d）" % (nlines, LOG_MAX_LINES))

        # ---- 本地 AI 模式 ----
        # 这里刻意**不**真的跑 demucs（要 import torch，几秒到几十秒，
        # 不该塞进冒烟测试）。只验两件必须在任何时候都成立的事：
        #   1. 模式在界面上选得到；
        #   2. 缺依赖时链路照常跑完并明确标注跳过了什么 —— 不能崩，
        #      也不能默默返回一堆没处理过的采样冒充"AI 处理过了"。
        rec("本地 AI 模式已注册",
            dsp.MODE_AI_LOCAL < len(dsp.MODE_NAMES) and local_ai is not None,
            "%s" % dsp.MODE_NAMES[dsp.MODE_AI_LOCAL])

        # ---- 「模块 × 引擎」两层结构 ----
        # 用户要求的核心：两个模块、每个模块两个引擎，四种组合在实时和文件
        # 两种输入下都要能用。这里验枚举齐整 + EngineChain 四种组合都能配、
        # 流式每次严格等长（demucs 用模拟缺失降级成直通，不 import torch）。
        combos = [(m, e) for m, engs in dsp.MODULE_ENGINES.items() for e in engs]
        rec("模块 × 引擎枚举齐整（2 × 2）",
            len(combos) == 4 and len(set(combos)) == 4 and
            all((m, e) in dsp.ENGINE_NAMES for m, e in combos),
            " / ".join("%s×%s" % c for c in combos))

        try:
            saved_env = local_ai._env_cache
            local_ai._env_cache = {"torch": (False, "模拟缺失"),
                                   "demucs": (False, "模拟缺失"),
                                   "onnxruntime": (False, "模拟缺失"),
                                   "cuda": (False, "模拟缺失"),
                                   "hq": (True, "内置")}
            eq_len = True
            detail = []
            for m, e in combos:
                ch = dsp.EngineChain(48000)
                ch.configure(module=m, engine=e, source=dsp.SRC_REALTIME,
                             semi=3.0, formant=1.0)
                for _ in range(6):
                    x = (np.random.randn(1024, 2) * 3000).astype(np.int16)
                    y = ch.process_stream(x)
                    if y.shape != x.shape:
                        eq_len = False
                detail.append("%s×%s:%.0fms" % (m, e, ch.latency_ms()))
            rec("EngineChain 四种组合流式严格等长", eq_len,
                "，".join(detail))
        except Exception as ex:
            rec("EngineChain 四种组合流式严格等长", False, repr(ex))
        finally:
            local_ai._env_cache = saved_env

        # 界面上切模块 / 切引擎 / 切输入都不能崩，且 cfg 字段齐
        try:
            bad = []
            for m, e in combos:
                app.v_module.set(m)
                app.v_engine.set(e)
                app._on_module_change()
                app._on_engine_change()
                app._on_source_change()
                c = app.current_cfg()
                for k in ("module", "engine", "source", "semi_exact",
                          "formant", "split", "ai"):
                    if k not in c:
                        bad.append("%s×%s 缺 %s" % (m, e, k))
            for s in (dsp.SRC_REALTIME, dsp.SRC_FILE):
                app.v_source.set(s)
                app._on_source_change()
            app.v_module.set(dsp.MODULE_PITCH)
            app.v_engine.set(dsp.ENGINE_FPGA_DIRECT)
            app._on_module_change()
            # 延迟说明那行 label 必须随时可刷（缺了会 AttributeError）
            app._refresh_ai_latency()
            app._refresh_labels()
            rec("界面切换模块/引擎/输入不崩且 cfg 字段齐", not bad,
                "; ".join(bad) if bad else "4 组合 + 2 输入方式全过")
        except Exception as ex:
            rec("界面切换模块/引擎/输入不崩且 cfg 字段齐", False, repr(ex))

        try:
            saved = local_ai._env_cache
            local_ai._env_cache = {"torch": (False, "模拟缺失"),
                                   "demucs": (False, "模拟缺失"),
                                   "onnxruntime": (False, "模拟缺失"),
                                   "cuda": (False, "模拟缺失"),
                                   "hq": (True, "内置")}
            n = 4800
            t = np.arange(n) / 48000.0
            x = np.empty((n, 2), dtype=np.float32)
            x[:, 0] = 0.3 * np.sin(2 * np.pi * 200.0 * t)
            x[:, 1] = x[:, 0] * 0.9
            y, ai = local_ai.run(x, 48000,
                                 dict(use_demucs=True, voice=local_ai.VOICE_HQ,
                                      semi=0.0, formant=1.0,
                                      vocal_gain=1.0, acc_gain=0.0))
            good = (y.shape == x.shape and np.all(np.isfinite(y))
                    and any("跳过" in s for s in ai["engine"]))
            rec("缺 demucs 时本地 AI 链路降级不崩且标注清楚", bool(good),
                ai["engine"][0] if ai["engine"] else "(无记录)")
        except Exception as e:
            rec("缺 demucs 时本地 AI 链路降级不崩且标注清楚", False, repr(e))
        finally:
            local_ai._env_cache = saved

        # 闲置的结果缓存应被自动释放
        app.proc_data = np.zeros((48000, 2), dtype=np.float32)
        app._proc_at = time.time() - PROC_TTL_SEC - 5
        freed, _dropped = app._clean_caches_now(silent=True)
        rec("闲置结果缓存自动释放", app.proc_data is None and freed > 0,
            "释放 %.2f MB" % app._mb(freed))

        # 刚用过的缓存不该被误删
        app.proc_data = np.zeros((48000, 2), dtype=np.float32)
        app._proc_at = time.time()
        app._clean_caches_now(silent=True)
        rec("刚用过的缓存不会被误删", app.proc_data is not None)

        app._housekeep()
        # 不再硬编码灯数（每加一盏就要改一次数字，改漏了就误报），
        # 改成断言"该有的检查项都在灯板上"
        want_led = {"dev_in", "dev_out", "sr", "engine", "sig_in", "sig_out",
                    "clip_in_l", "clip_in_r", "clip_out_l", "clip_out_r",
                    "buf", "dsp", "ai", "file", "cache", "sw"}
        rec("GUI 构建", want_led.issubset(set(app.leds.state.keys())),
            "%d 盏灯" % n_led)
        app._refresh_devices()
        app.destroy()
        rec("GUI 刷新", True, "poll / 频谱 / 灯板 / 巡检 / 设备刷新均无异常")
    except Exception as e:
        rec("GUI 构建", False, repr(e))

    lines.append("RESULT: %s" % ("ALL PASS" if ok else "HAS FAILURE"))
    txt = "\n".join(lines) + "\n"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass
    return 0 if ok else 1


def _ai_check(out_path="_ai_check.txt"):
    """真跑一次 demucs 并把结果写文件（--ai-check）。

    常规自检（`--selftest`）刻意**不** import torch —— 那要几秒到几十秒，
    不该塞进冒烟测试。但"打包后的 EXE 到底能不能跑 AI"这件事必须能验，
    否则分发之后才发现 torch 没打进去就晚了。所以单独开一个开关。
    """
    lines = ["=== AI 分离能力检查（真跑 demucs）===", ""]
    ok = True

    def rec(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        lines.append("[%s] %s %s" % ("PASS" if good else "FAIL", name, detail))

    try:
        import numpy as np
        import local_ai
        # 轻量 EXE 不含 torch：先把本机装 torch 的 site-packages 挂进 sys.path，
        # 下面 __import__("torch") 才能成功（冻结解释器与本机 torch 同 ABI）。
        local_ai.ensure_local_runtime()
        # ★ 必须动态 import：写成 `import torch` 的话 PyInstaller 的 AST 扫描
        #   会把它当成硬依赖，--exclude-module torch 就失效了 —— 实测轻量版
        #   EXE 会从 54 MB 涨到 2.2 GB。
        torch = __import__("torch")
        rec("torch 可导入", True, torch.__version__)
        rec("CUDA 可用", torch.cuda.is_available(),
            torch.cuda.get_device_name(0) if torch.cuda.is_available() else "无")
    except Exception as e:
        rec("torch 可导入", False, "%s: %s" % (type(e).__name__, e))
        lines.append("RESULT: FAIL")
        _write_lines(lines, out_path)
        return 1

    try:
        import local_ai
        r_ok, r_why = local_ai.demucs_ready()
        rec("demucs 就绪", r_ok, r_why or local_ai.summary_text())
    except Exception as e:
        rec("demucs 就绪", False, "%s: %s" % (type(e).__name__, e))
        lines.append("RESULT: FAIL")
        _write_lines(lines, out_path)
        return 1

    # ★ 关键的一条：上面三项走的是 local_ai 自己的探测（会先挂载 torch），
    #   **不代表主流程真的会启用 AI**。2026-10-23 那次事故就是三项全绿、
    #   而 _split_worker 因为用了 import 时冻结的 HAVE_AI 而永远走 DSP。
    #   所以这里直接问主流程那个开关本身。
    try:
        g_ok, g_why = _ai_ready()
        rec("主流程 AI 开关（_ai_ready）", g_ok,
            g_why or "已启用：分离会走 demucs（HAVE_AI 占位=%r，不参与判定）"
            % (HAVE_AI,))
    except Exception as e:
        rec("主流程 AI 开关（_ai_ready）", False,
            "%s: %s" % (type(e).__name__, e))

    try:
        sr = 44100
        n = int(6 * sr)
        t = np.arange(n) / float(sr)
        x = (0.3 * np.sin(2 * np.pi * 220 * t)
             + 0.15 * np.random.RandomState(0).randn(n)).astype(np.float32)
        x = np.ascontiguousarray(np.stack([x, x], axis=1))
        import time
        t0 = time.time()
        voc, acc, info = local_ai.separate(x, sr, device="auto",
                                           quality=local_ai.QUALITY_STD)
        dt = time.time() - t0
        rec("真实分离跑通", voc.shape[0] == n and acc.shape[0] == n,
            "%.1f s / 6 s 音频，device=%s" % (dt, info.get("device")))
        rec("跑在 CUDA 上", str(info.get("device", "")).startswith("cuda"),
            "device=%s" % info.get("device"))
    except Exception as e:
        rec("真实分离跑通", False, "%s: %s" % (type(e).__name__, e))

    lines.append("RESULT: %s" % ("ALL PASS" if ok else "HAS FAILURE"))
    _write_lines(lines, out_path)
    return 0 if ok else 1


def _write_lines(lines, out_path):
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _diag():
    """写一份"这台机器上到底能不能写文件"的自检报告。

        ACX720VoiceChanger.exe --diag

    为什么需要它：用户环境里出现的 `PermissionError(13, 'Permission denied')`
    在开发机上永远复现不出来（同一路径、同一账号，手工写就成功）。这种
    差异只能靠**在出问题的那台机器、那个进程里**逐项试写才能定位。
    报告写到 exe 旁边的 _diag.txt（写不进去就退到 %TEMP%），内容全是
    实测结果，没有推断。
    """
    import tempfile
    lines = []
    add = lines.append
    add("ACX720 变声器 环境自检  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("=" * 60)
    add("frozen            = %s" % getattr(sys, "frozen", False))
    add("sys.executable    = %s" % sys.executable)
    add("__file__          = %s" % globals().get("__file__", "?"))
    add("cwd               = %s" % os.getcwd())
    add("LOCALAPPDATA      = %s" % os.environ.get("LOCALAPPDATA"))
    add("TEMP              = %s" % os.environ.get("TEMP"))
    add("python            = %s" % sys.version.replace("\n", " "))
    add("日志候选          = %s" % (_LOG_TRIES or "（还没写过日志）"))
    add("日志实际路径      = %s" % log_path())
    add("")
    add("--- 逐目录写测试（建 1 KB 文件 → 读回 → 删除）---")
    targets = [
        ("桌面", os.path.join(os.path.expanduser("~"), "Desktop")),
        ("下载", os.path.join(os.path.expanduser("~"), "Downloads")),
        ("文档", os.path.join(os.path.expanduser("~"), "Documents")),
        ("音乐", os.path.join(os.path.expanduser("~"), "Music")),
        ("临时目录", tempfile.gettempdir()),
        ("程序目录", _app_dir()),
        ("日志目录", LOG_DIR),
    ]
    for name, d in targets:
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "_acx720_diag_%d.tmp" % os.getpid())
            with open(p, "wb") as f:
                f.write(b"x" * 1024)
            with open(p, "rb") as f:
                n = len(f.read())
            os.remove(p)
            add("  %-8s OK      %s（写入 %d 字节）" % (name, d, n))
        except Exception as e:
            add("  %-8s FAIL    %s → %r" % (name, d, e))
    add("")
    add("--- 走真实写盘函数 audio_io.save_audio（下载目录，1 秒静音）---")
    try:
        import audio_io
        import numpy as np
        p = os.path.join(os.path.expanduser("~"), "Downloads",
                         "_acx720_diag_%d.wav" % os.getpid())
        audio_io.save_audio(p, np.zeros((44100, 2), dtype=np.float32), 44100)
        sz = os.path.getsize(p)
        os.remove(p)
        add("  OK      %s（%d 字节）" % (p, sz))
    except Exception as e:
        add("  FAIL    %r" % (e,))
        import traceback
        add(traceback.format_exc().rstrip())
    txt = "\n".join(lines) + "\n"

    out = None
    for d in (_app_dir(), tempfile.gettempdir()):
        try:
            p = os.path.join(d, "_diag.txt")
            with open(p, "w", encoding="utf-8", errors="replace") as f:
                f.write(txt)
            out = p
            break
        except Exception:
            continue
    try:
        print(txt)
    except Exception:
        pass
    if out:
        file_log("DIAG 已写入 %s" % out)
    return 0


def _run_identity():
    """启动时打一行"我现在跑的是哪个版本"。

    ★ 2026-10-23：打包出来的 EXE 是**源码的一份快照**。改完 host/*.py 却没重新
      打包时，双击 EXE 跑的还是旧代码 —— 界面上的报错会和源码里早就修掉的那个
      一模一样，而且从界面上完全看不出区别（"文件变声还是原来那种报错"就是这么
      来的：EXE 建于 22:59，而修复在 23:52~23:53 才写进源码）。
      把"跑的是 EXE 还是源码 + 文件时间"打在日志第一行，一眼就能分辨。
    """
    def _ts(p):
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
        except Exception:
            return "?"

    if getattr(sys, "frozen", False):
        return ("运行方式：打包 EXE（构建于 %s）—— 源码改了要重新打包才会生效\n"
                "            %s" % (_ts(sys.executable), sys.executable))

    here = os.path.abspath(__file__)
    newest, newest_t = here, os.path.getmtime(here)
    for name in ("audio_io.py", "player.py", "dsp_core.py", "host_app.py"):
        q = os.path.join(os.path.dirname(here), name)
        try:
            t = os.path.getmtime(q)
            if t > newest_t:
                newest, newest_t = q, t
        except Exception:
            pass
    return "运行方式：源码（最新改动 %s @ %s）" % (os.path.basename(newest), _ts(newest))


def _mode_json_path():
    """上位机 → 声卡桥的「当前模式」小文件。

    为什么用文件而不是管道：声卡桥可能是本程序拉起来的（有 pipe），
    也可能是用户自己双击 bat 起来的（没有 pipe）。文件对两种情况都成立，
    而且掉线重连后自动就同步了。
    两边必须用**同一条规则**算路径：桥那边是 tool/logs/mode.json，
    这里用 _project_root() 找同一个地方。
    """
    root = _project_root()
    if root:
        d = os.path.join(root, "tool", "logs")
    else:
        d = _app_dir()
    return os.path.join(d, "mode.json")


def _writable_summary():
    """一行说清"当前进程能不能往用户目录写"。

    ★ 2026-10-23：实测存在"进程能读能跑、但往桌面/下载/文档写一律
      PermissionError(13)"的环境（受限令牌 / 沙箱里启动 EXE 就是这样）。
      这时不是软件坏了，而是这个进程没有写权限。提前一行说清楚，
      比让用户在处理完成时撞一句 PermissionError 好得多。
    """
    bad = []
    for label, sub in (("桌面", "Desktop"), ("下载", "Downloads")):
        d = os.path.join(os.path.expanduser("~"), sub)
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, "_acx720_probe_%d.tmp" % os.getpid())
            with open(t, "wb") as f:
                f.write(b"x")
            os.remove(t)
        except Exception as e:
            bad.append("%s（%s）" % (label, type(e).__name__))
    if bad:
        return ("⚠ 当前进程写不了：" + "、".join(bad) +
                "。输出会自动改存到程序目录的「输出」子文件夹；"
                "要存到原位置，请关掉后用资源管理器重新启动（必要时以管理员身份）。")
    return "写入检查：桌面 / 下载 均可写"


def main():
    if "--diag" in sys.argv:
        sys.exit(_diag())
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--ai-check" in sys.argv:
        sys.exit(_ai_check())
    app = App()
    app._log(_run_identity())
    app._log(_writable_summary())
    app._log("上位机就绪。先确认参数面板，再切到「实时变声」或「文件变声」。")
    app.mainloop()


if __name__ == "__main__":
    main()
