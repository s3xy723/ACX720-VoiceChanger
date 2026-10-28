# -*- coding: utf-8 -*-
"""
local_ai.py —— 本地 AI 处理引擎（全程在本机跑，不联网、不上传）

这个模块解决什么事
------------------
"变声"这件事有两个完全不同的层次，之前是混在一起的：

    1. 从一首混音里把人声挖出来   —— 这是**信息量**问题
    2. 把挖出来的人声换个音色     —— 这是**变换**问题

第 1 步靠"左右声道共有成分"这种纯 DSP 手段是做不到的：贝斯、底鼓、
居中的钢琴一样是居中的，中置提取会连它们一起搬走。这一步必须有
真实的模型先验，demucs 就是干这个的（合成基准上人声提取 +19 dB
SI-SDR，不是参数调得好，是它见过几十万首歌）。

第 2 步才是变调 / 变音色，可以用 DSP（hq_voice 的频域变调 +
共振峰保持），也可以用神经网络（RVC 类模型）。

所以"本地 AI 处理"这条链路是：

        输入混音
          │
          ├─ [AI]  demucs 分离 ──▶ 人声 + 伴奏        （真实神经网络）
          │
          ├─ [DSP 或 AI]  人声处理                     （内置 HQ 变调 / RVC）
          │
          └─ 重新混合 ──▶ 输出

界面上会把每一步标清楚是 AI 还是 DSP —— 不想让人以为"AI 处理"四个字
下面全是神经网络。

关于依赖
--------
torch / demucs / onnxruntime 都不打进 EXE（几百 MB ~ 2 GB）。
所以这里的规矩和 ai_separate.py 一样：**有就用，没有就置灰并说明原因**，
绝不因为缺依赖而崩。probe() 会给出每一项的准确状态。

★ 音频重采样用的是 hq_voice 的带限 sinc 核，不是 audio_io 的线性插值。
  demucs 要求 44.1 kHz，而用户文件可能是 48 kHz；线性插值在这里会把
  高频的镜像折叠回来，等于先给模型喂了一份带噪声的输入。
"""

import os
import sys
import glob
import threading

import numpy as np

import hq_voice


# ---------------------------------------------------------------- 常量

VOICE_NONE = "none"
VOICE_HQ = "hq"
VOICE_RVC = "rvc"

VOICE_NAMES = [
    (VOICE_NONE, "不处理（只做人声分离 / 提取）"),
    (VOICE_HQ, "内置高质量变调（频域 + 共振峰保持）"),
    (VOICE_RVC, "RVC 音色转换（需自备模型）"),
]

DEMUCS_MODEL = "htdemucs"
# 4 个微调模型的集成（demucs 官方 fine-tuned bag）。同一段 45 s 真实歌曲实测：
#   htdemucs     伴奏段人声残留 −61.9 dB
#   htdemucs_ft  伴奏段人声残留 −70.3 dB   ← 低 8.4 dB，且不改变音色
# 代价是 4 倍推理时间（RTF 0.42 → 1.33，RTX 5070 Ti）。
# 权重约 320 MB，首次使用会从 HF 镜像下载。
DEMUCS_MODEL_FT = "htdemucs_ft"

# 分离质量档位。shifts 是"时间平移等变平均"：把输入随机平移后推理、把输出
# 反平移，重复 N 次取平均。demucs 官方说法是能提升约 0.2 dB SDR，代价是 N 倍
# 时间。overlap 是分段推理时相邻段的重叠比例，太小会在段边界留下可听的接缝。
QUALITY_FAST = "fast"
QUALITY_STD = "std"
QUALITY_HIGH = "high"

# ★ shifts 特意不用 1。demucs 的实现里 shifts>0 会给输入加一个**随机**时间
#   平移再反平移；shifts=1 时只平移一次、不做任何平均，SDR 一点没涨，却让
#   每次跑出来的结果都不一样（同一首歌跑两遍尾部会有微小差异，测试没法复现）。
#   要提升是靠 shifts>=2 的多次平均，所以 0 和 4 之间没有什么中间值可选。
#
# ★ 高质档改成"换模型"而不是"加 shifts"。原来 high = htdemucs + shifts=4，
#   实测只比 std 好不到 1 dB，却贵一倍（4 次 vs 2 次推理）。改成
#   htdemucs_ft（4 模型集成）后，实测残留低 8.4 dB，耗时与 shifts=4 相当 ——
#   同样的时间预算，换模型比加平移平均划算得多。
QUALITY_PRESETS = {
    QUALITY_FAST: dict(shifts=0, overlap=0.10, model=DEMUCS_MODEL),
    # 文件模式默认档：2 次平移平均 + 50% 段重叠。比旧的 0/25% 明显更干净，
    # 代价约 2~4 倍推理时间；RTX 5070 Ti 上 5 秒素材仍是秒级。
    QUALITY_STD: dict(shifts=2, overlap=0.50, model=DEMUCS_MODEL),
    QUALITY_HIGH: dict(shifts=2, overlap=0.50, model=DEMUCS_MODEL_FT),
}

QUALITY_NAMES = [
    (QUALITY_FAST, "快速（段重叠 10%）"),
    (QUALITY_STD, "标准（2× 平移平均，段重叠 50%）"),
    (QUALITY_HIGH, "高质（htdemucs_ft 四模型集成，残留更低 · 慢 3 倍）"),
]

_env_cache = None
_env_lock = threading.Lock()


# ---------------------------------------------------------------- 本机 torch 运行时挂载
#
# 轻量版 EXE 故意不把 torch/demucs 打进去（否则 2.2 GB 起步）。但它和本机
# 装 torch 的解释器是**同一个 CPython 3.14**（ABI 完全一致），所以运行时把
# 本机装 torch 的那个 site-packages 挂进 sys.path，冻结 EXE 就能直接
# import 本机的 torch / demucs / huggingface_hub —— 既不涨包体，又能在本机
# 真跑 GPU 分离。换到没装 torch 的机器上，这里会返回失败，界面给出明确引导。

_RUNTIME_PREPARED = False
_RUNTIME_NOTE = ""


def _torch_search_paths():
    """可能的 torch 安装位置，按优先级去重返回绝对路径列表。"""
    paths = []
    # 1) 当前解释器的 user site（本机实测 torch 就装在这儿）
    try:
        import site
        usp = site.getusersitepackages()
        if isinstance(usp, (list, tuple)):
            paths.extend(usp)
        elif usp:
            paths.append(usp)
    except Exception:
        pass
    # 2) 用户级 Python 目录下的所有 site-packages（覆盖 3.10~3.14）
    roaming = os.path.expanduser(r"~\AppData\Roaming\Python")
    if os.path.isdir(roaming):
        for d in sorted(os.listdir(roaming), reverse=True):
            p = os.path.join(roaming, d, "site-packages")
            if os.path.isdir(p):
                paths.append(p)
    # 3) 系统 Python
    for base in ("C:/Python314", "C:/Python313", "C:/Python312",
                 "C:/Python311", "C:/Python310"):
        p = base + "/Lib/site-packages"
        if os.path.isdir(p):
            paths.append(p)
    # 4) workbuddy 管理的 venv
    wb = r"C:/Users/s3xy/.workbuddy/binaries/python/envs"
    if os.path.isdir(wb):
        for d in sorted(os.listdir(wb), reverse=True):
            p = os.path.join(wb, d, "Lib", "site-packages")
            if os.path.isdir(p):
                paths.append(p)
    seen, out = set(), []
    for p in paths:
        try:
            ap = os.path.abspath(p)
        except Exception:
            ap = p
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def ensure_local_runtime():
    """把本机含 torch 的 site-packages 挂进 sys.path（幂等）。

    返回 (是否可用, 说明)。可用=True 时，后续 import torch/demucs 都从这来。
    """
    global _RUNTIME_PREPARED, _RUNTIME_NOTE
    if _RUNTIME_PREPARED:
        return True, _RUNTIME_NOTE
    # 已有 torch 在 sys.path（开发环境 / onedir）直接算成功
    try:
        __import__("torch")
        _RUNTIME_PREPARED, _RUNTIME_NOTE = True, "torch 已在 sys.path"
        return True, _RUNTIME_NOTE
    except Exception:
        pass
    note = "本机未找到 torch"
    # torch 在用户级 site-packages，demucs/huggingface_hub 在 venv 的
    # site-packages —— 两者不在同一目录。把所有含 AI 组件的 site-packages 都挂上，
    # 再统一 import，避免"torch 找到了但 demucs 找不到"。
    for p in _torch_search_paths():
        if not os.path.isdir(p):
            continue
        has_ai = any(os.path.isdir(os.path.join(p, m))
                     for m in ("torch", "demucs", "huggingface_hub",
                               "onnxruntime"))
        if not has_ai:
            continue
        if p not in sys.path:
            # 前置：用本机**完整**的 numpy（含 core.multiarray 兼容垫片）顶替
            # 冻结 EXE 自带的、缺该垫片的 numpy，否则 torch/demucs 一跑就报
            # No module named 'numpy.core.multiarray'。只影响 site-packages 里的
            # 包（torch/numpy/demucs…），不碰标准库；且这些包在 AI 准备阶段才
            # 挂上，App 启动时已加载的模块（sys.modules）不会被重新导入。
            sys.path.insert(0, p)
        # 冻结 EXE 的 stdlib 是残缺子集，外部 torch 可能 import 到没打进包的
        # 标准库（如 timeit）—— 把同版本的完整标准库目录也挂上，避免缺模块
        _maybe_add_stdlib(p)
    # 候选都挂上后，再试 import
    try:
        __import__("torch")
        __import__("demucs")
        _RUNTIME_PREPARED, _RUNTIME_NOTE = True, "已挂载本机 AI 运行时（torch+demucs）"
        return True, _RUNTIME_NOTE
    except Exception as e:
        note = "挂载本机 AI 运行时失败：%s" % e
    _RUNTIME_PREPARED = True           # 标记已查过，避免反复扫
    _RUNTIME_NOTE = note
    return False, note


def _maybe_add_stdlib(torch_site_packages):
    """从 torch 的 site-packages 路径反推同版本 Python 的标准库目录并挂载。

    冻结 EXE 只打包了标准库的一个子集；外部 torch 可能 import 到没打进包的
    标准库模块（如 timeit）。把对应版本（例如 C:/Python314/Lib）的完整标准库
    挂到 sys.path 末尾，缺哪些标准库模块都能从这儿补，又不干扰已打包的部分。
    ★ 只挂**同版本**标准库：绝不能把 3.10/3.12 的混进来，否则会被 3.14 解释器
      加载、直接崩。
    """
    parts = torch_site_packages.replace("\\", "/").split("/")
    ver = None
    for seg in parts:
        if seg[:6] == "Python" and seg[6:].isdigit():
            ver = seg
            break
    if ver:
        lib = "C:/%s/Lib" % ver
        if os.path.isdir(lib) and lib not in sys.path:
            sys.path.append(lib)
        return
    # 反推不到版本时，扫描本机 C:/Python3XX（仍只挂存在的）
    for d in ("C:/Python310", "C:/Python311", "C:/Python312",
             "C:/Python313", "C:/Python314"):
        lib = d + "/Lib"
        if os.path.isdir(lib) and lib not in sys.path:
            sys.path.append(lib)


def runtime_note():
    return _RUNTIME_NOTE


def ensure_model_downloaded(progress=None, name=DEMUCS_MODEL):
    """强制把 htdemucs 权重拉到本地缓存（已缓存则秒回）。

    demucs 4.x 通过 huggingface_hub 拉权重；国内直连常被掐，ensure_model_endpoint
    会把端点切到镜像。这一步可能要下载约 150 MB，progress 回调用于刷新界面。
    """
    ensure_local_runtime()
    ensure_model_endpoint()
    if progress:
        progress("正在准备 AI 模型权重（首次需下载约 150 MB）…")
    from demucs.pretrained import get_model
    m = get_model(name)
    m.eval()
    if progress:
        progress("AI 模型权重已就绪")
    return m


# ---------------------------------------------------------------- 环境自检

def _try_import(name):
    try:
        m = __import__(name)
        return True, str(getattr(m, "__version__", "?"))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def probe(force=False):
    """返回 {名字: (是否可用, 说明)}。结果会缓存，force=True 强制重查。"""
    global _env_cache
    with _env_lock:
        if _env_cache is not None and not force:
            return dict(_env_cache)

        ensure_local_runtime()      # ★ 先挂本机 torch，再探测
        env = {}
        env["torch"] = _try_import("torch")
        env["demucs"] = _try_import("demucs")
        env["onnxruntime"] = _try_import("onnxruntime")

        ok, dev = False, ""
        if env["torch"][0]:
            try:
                torch = __import__("torch")
                ok = bool(torch.cuda.is_available())
                if ok:
                    gb = torch.cuda.get_device_properties(0).total_memory / (1024.0 ** 3)
                    dev = "%s，%.1f GB 显存" % (torch.cuda.get_device_name(0), gb)
                else:
                    dev = "没有可用的 CUDA 设备，将退回 CPU（约慢 5~15 倍）"
            except Exception as e:
                dev = repr(e)
        env["cuda"] = (ok, dev)

        env["hq"] = (True, "内置，只依赖 numpy")
        _env_cache = env
        return dict(env)


def demucs_ready():
    e = probe()
    return bool(e["torch"][0] and e["demucs"][0]), (
        "" if (e["torch"][0] and e["demucs"][0])
        else "缺少 %s" % ("、".join(k for k in ("torch", "demucs") if not e[k][0])))


# ---------------------------------------------------------------- 权重下载引导

# demucs 4.x 通过 huggingface_hub 从 huggingface.co 拉 htdemucs 的权重。
# ★ 这是国内网络上一个真实的、会让人以为"AI 模式根本没做"的坑：
#   huggingface.co 经常被掐（实测 schannel 在 SSL 握手阶段就失败），
#   demucs 会连着重试 5 次再抛错，界面上看到的就是"分离卡住 / 一直转圈"。
#   权重一旦进了本地缓存就再也不需要联网，所以只在缓存缺失时才处理。
HF_MIRROR = "https://hf-mirror.com"
HF_OFFICIAL = "https://huggingface.co"
HF_REPO = "adefossez/HTDemucs"

_hf_note = ""


def _hf_weights_cached():
    """本地缓存里是否已经有 htdemucs 的权重（>10 MB 的文件）。"""
    try:
        from huggingface_hub import try_to_load_from_cache
        for fn in ("955717e8.safetensors", "htdemucs.th", "955717e8-8726e21a.th"):
            r = try_to_load_from_cache(HF_REPO, fn)
            if isinstance(r, str) and os.path.exists(r):
                return True
    except Exception:
        pass
    # 上面那招在某些版本上不可用，退回目录扫描（含老版 torch.hub 的位置）
    home = os.path.expanduser("~")
    cand = [os.path.join(home, ".cache", "huggingface", "hub",
                         "models--adefossez--HTDemucs"),
            os.path.join(home, ".cache", "torch", "hub", "checkpoints")]
    for p in cand:
        if not os.path.isdir(p):
            continue
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    if os.path.getsize(os.path.join(root, f)) > 10 * 1024 * 1024:
                        return True
                except OSError:
                    pass
    return False


def _can_reach(url, timeout=5.0):
    """能不能真正从该 URL 拿到内容（HTTP 状态 2xx/3xx 才算）。

    ★ 不能只做 TCP+TLS 握手：实测 huggingface.co 能握手，但 HEAD 返回
      502（被中间网络挡了）。旧实现把 502 当成"能连"，于是不切镜像，
      demucs 就会一直重试 5 次然后失败 —— 界面上就是"AI 一直转圈"。
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= int(r.status) < 400
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def ensure_model_endpoint():
    """确保 demucs 拿得到权重；拿不到时把 huggingface_hub 切到国内镜像。

    ★ 必须在 import demucs.pretrained / huggingface_hub **之前**调用：
      HF_ENDPOINT 是 huggingface_hub 在 import 时读进常量的，晚了就不生效。

    返回实际使用的端点（None = 用官方默认）。幂等，可以反复调。
    """
    global _hf_note
    cur = os.environ.get("HF_ENDPOINT")
    if cur:
        _hf_note = "自定义权重端点 %s" % cur
        return cur
    if _hf_weights_cached():
        _hf_note = ""
        return None
    if _can_reach(HF_OFFICIAL):
        _hf_note = ""
        return None
    os.environ["HF_ENDPOINT"] = HF_MIRROR
    _hf_note = "huggingface.co 直连不通，已改用镜像 %s" % HF_MIRROR
    return HF_MIRROR


def endpoint_note():
    """给界面看的一句说明（空串 = 一切正常，不需要打扰用户）。"""
    return _hf_note


def summary_text():
    """给界面用的一行状态文字。"""
    e = probe()
    bits = []
    ok, _ = demucs_ready()
    bits.append("demucs %s" % ("可用" if ok else "不可用"))
    bits.append("CUDA %s" % ("可用" if e["cuda"][0] else "不可用"))
    if e["cuda"][0]:
        bits.append("（%s）" % e["cuda"][1])
    if _hf_note:
        bits.append("（%s）" % _hf_note)
    return "本地 AI 环境：" + "，".join(bits)


# ---------------------------------------------------------------- 高质量重采样

# AI 链路的重采样抽头数（hq_voice 里默认 48）。
# 实测 48 kHz -> 44.1 kHz 时 23 kHz 成分（会折叠到 21.1 kHz）的抑制：
#     48 抽头 -17.5 dB   64 抽头 -23.6 dB   96 抽头 -41.5 dB   128 抽头 -82.5 dB
#   对照：scipy.signal.resample_poly 默认（Kaiser beta=5）只有 -11.2 dB，
#        audio_io 的线性插值 -6.9 dB
# 取 96：比参考实现好 30 dB，代价是 4 分钟立体声约 20 秒/方向。
# 再往上加是在 23 kHz 这个真实音乐里几乎没有能量的频段上砸时间。
RESAMP_TAPS_AI = 96


def resample_to(x, sr_in, sr_out, taps=RESAMP_TAPS_AI):
    """带限重采样。x 可以是 (n,) 或 (n,c)。"""
    sr_in = int(sr_in)
    sr_out = int(sr_out)
    if sr_in == sr_out:
        return np.asarray(x, dtype=np.float64)
    a = np.asarray(x, dtype=np.float64)
    one_d = (a.ndim == 1)
    if one_d:
        a = a.reshape(-1, 1)
    n_out = int(round(a.shape[0] * float(sr_out) / float(sr_in)))
    if n_out < 1:
        n_out = 1
    # ratio > 1 表示"变短"（抽取），sinc_resample 会自己把截止频率降下来抗混叠
    ratio = float(a.shape[0]) / float(n_out)
    pos = np.arange(n_out, dtype=np.float64) * ratio

    # ★ 尾部补零，而不是把 pos 裁到 n_in-1。
    #   升采样时最后一个位置是 n_in - ratio > n_in-1，一旦 clip，位置序列
    #   就不再等间距，sinc_resample 会退回逐点慢通路（实测慢 10 倍以上），
    #   而且尾部会变成一长串重复值。补零既保住等间距，也让超出输入末端的
    #   输出样本自然淡出。
    nt = int(np.ceil((taps // 2) * max(1.0, ratio))) + 2
    pad = np.concatenate([a, np.zeros((nt, a.shape[1]), dtype=np.float64)])

    out = np.empty((n_out, a.shape[1]), dtype=np.float64)
    for c in range(a.shape[1]):
        out[:, c] = hq_voice.sinc_resample(np.ascontiguousarray(pad[:, c]), pos,
                                           max(ratio, 1e-9), taps=taps)
    return out[:, 0] if one_d else out


# ---------------------------------------------------------------- demucs 分离

_model = None
_model_name = None
_model_dev = None
_model_lock = threading.Lock()


def _get_model(name=DEMUCS_MODEL, dev="cpu"):
    """拿到 eval 态的模型，并搬到 dev 上。

    ★ apply_model 只把**输入**搬到 device，**模型**必须由调用方 .to(device)，
      否则模型留在 CPU 上、输入在 CUDA 上，直接报 device mismatch。
    """
    global _model, _model_name, _model_dev
    with _model_lock:
        if _model is None or _model_name != name:
            # ★ 必须在 import demucs 之前：HF_ENDPOINT 是 huggingface_hub
            #   在 import 时读进常量的，设晚了不生效。
            ensure_model_endpoint()
            ensure_local_runtime()   # ★ 确保 import demucs 能找到本机安装
            from demucs.pretrained import get_model
            m = get_model(name)
            m.eval()
            _model, _model_name, _model_dev = m, name, None
        if _model_dev != dev:
            _model.to(dev)
            _model_dev = dev
    return _model


def _pick_device(pref):
    torch = __import__("torch")
    if pref == "cuda" or pref == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if pref == "cuda":
            return "cuda"          # 用户强制指定，让它自己报错，别偷偷改
    return "cpu"


MIN_SEGMENT = 3.0        # 显存不足时分段能缩到的下限（秒）


def _model_segment(model):
    """模型训练时的分段长度（秒）。htdemucs 自带的是 7.8 s。

    ★ 这个值只能改小，不能改大。
      HTDemucs.forward 内部按 **self.segment** 算 training_length 并据此
      reshape 给 cross-transformer；而 apply_model 是按**传入的** segment
      决定补零长度。两者不一致时，传进来的样本数除不尽 training_length，
      forward 直接崩（实测传 10.0 s 时）：
          RuntimeError: shape '[1, 4, -1, 343980]' is invalid for input of
          size 3528000
      （343980 = 7.8 s × 44100 Hz 就是模型自己的值。传 7.8 或 None 都正常。）
      改小是安全的：补零只会更长，reshape 的基准仍是模型自己的那个数。
    """
    subs = getattr(model, "models", None)
    for m in (list(subs) if subs else [model]):
        s = getattr(m, "segment", None)
        if s:
            try:
                v = float(s)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    return None


def separate(x, sr, device="auto", quality=QUALITY_STD, progress=None,
             segment=None, with_drums=False):
    """把立体声混音拆成 (人声, 伴奏)，形状都是 (n, 2)，与输入等长同采样率。

    device     : "auto" / "cuda" / "cpu"
    quality    : QUALITY_FAST / QUALITY_STD / QUALITY_HIGH
    segment    : 强制指定分段长度（秒）。一般不管，用模型自带的值；
                 只有显存不够时才会往小调。
    with_drums : True 时多返回一个 drums 源，返回四元组 (人声, 伴奏, 鼓, info)。
                 只给评测工具用（tool/sep_eval.py 要拿鼓轨量漏音），主程序不用。

                 ★ 曾经用它给 drums 做侧链去压"人声轨里的鼓残留"，实测是负收益，
                   已删掉，别再捡回来：那条 Wiener 掩蔽把人声自己的低频厚度砍掉
                   5 dB，只换来高频 0.3 dB —— 听感就是变薄、变闷、忽大忽小。
    """
    torch = __import__("torch")
    from demucs.apply import apply_model

    a = np.asarray(x, dtype=np.float32)
    if a.ndim == 1:
        a = np.repeat(a.reshape(-1, 1), 2, axis=1)
    n = a.shape[0]

    preset = dict(QUALITY_PRESETS.get(quality, QUALITY_PRESETS[QUALITY_STD]))
    dev = _pick_device(device)

    # 质量档可以换模型：高质档用 htdemucs_ft（4 模型集成）。
    # 它的权重要现下（约 320 MB），HF 镜像不通或磁盘满时不能让整次分离
    # 失败 —— 退回主干模型，把原因如实记进 info，用户能看见。
    model_name = preset.get("model", DEMUCS_MODEL)
    try:
        model = _get_model(model_name, dev)
    except Exception as e:
        if model_name == DEMUCS_MODEL:
            raise
        if progress:
            progress("%s 加载失败（%s），退回 %s" % (model_name, e, DEMUCS_MODEL))
        model_name = DEMUCS_MODEL
        model = _get_model(model_name, dev)
    msr = int(getattr(model, "samplerate", 44100))

    # 分段长度：优先用模型自带的（不能改大，理由见 _model_segment）
    mseg = _model_segment(model)
    if segment:
        seg = float(segment)
    elif mseg:
        seg = mseg
    else:
        seg = 10.0
    if mseg:
        seg = min(seg, mseg)

    work = a
    if int(sr) != msr:
        if progress:
            progress("重采样 %d Hz -> %d Hz（带限 sinc）" % (int(sr), msr))
        work = resample_to(a, int(sr), msr).astype(np.float32)

    wav = torch.from_numpy(np.ascontiguousarray(work.T)).float()
    ref = wav.mean(0)
    std = ref.std() + 1e-8
    wn = (wav - ref.mean()) / std

    # ---- demucs 的进度回调：每个分段 start/end 各来一次，带 segment_offset ----
    state = {"seg": seg}
    total = max(1, wn.shape[-1])
    last_pct = [-1]

    def _cb(d):
        if not progress or d.get("state") != "start":
            return
        off = int(d.get("segment_offset", 0))
        pct = int(min(100, 100.0 * (off + state["seg"] * msr) / total))
        if pct != last_pct[0]:
            last_pct[0] = pct
            progress("demucs 推理中（%s，%d%%）" % (dev, pct))

    def _infer(use_dev, use_seg):
        # ★ 这里必须跟着 model_name 走，不能写死 DEMUCS_MODEL：
        #   高质档用的是 htdemucs_ft，OOM 回退换 device 时若加载主干模型，
        #   "高质"就悄悄变成"标准"了。
        md = _get_model(model_name, use_dev)
        with torch.no_grad():
            return apply_model(md, wn[None],
                               shifts=preset["shifts"],
                               split=True,
                               overlap=preset["overlap"],
                               progress=False,
                               device=use_dev,
                               segment=use_seg,
                               callback=_cb)[0]

    if progress:
        progress("demucs 推理中（%s，%s 档 / %s，分段 %.1f s）…"
                 % (dev, quality, model_name, seg))

    out = None
    try:
        out = _infer(dev, seg)
    except RuntimeError as e:
        msg = str(e).lower()
        oom = ("out of memory" in msg) or ("cufft" in msg)
        if not (oom and dev == "cuda"):
            raise
        # 显存不够：清缓存 + 缩短分段重试；再不够就整个退回 CPU
        if progress:
            progress("CUDA 显存不足，缩短分段重试…")
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            seg = max(MIN_SEGMENT, seg / 2.0)
            state["seg"] = seg
            out = _infer("cuda", seg)
        except RuntimeError:
            if progress:
                progress("CUDA 仍不足，退回 CPU（约慢 5~15 倍）…")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            dev = "cpu"
            seg = max(MIN_SEGMENT, seg / 2.0)
            state["seg"] = seg
            out = _infer("cpu", seg)

    out = out.float().cpu().numpy()
    out = out * float(std) + float(ref.mean())

    names = list(getattr(model, "sources", ["drums", "bass", "other", "vocals"]))
    vi = names.index("vocals") if "vocals" in names else len(names) - 1
    voc = out[vi].T.astype(np.float32)
    acc = (out.sum(0) - out[vi]).T.astype(np.float32)
    # drums 只在评测工具要量漏音时才给出来（tool/sep_eval.py），保持三元组接口不变。
    drum = None
    if with_drums:
        di = names.index("drums") if "drums" in names else -1
        if di >= 0:
            drum = out[di].T.astype(np.float32)

    if int(sr) != msr:
        if progress:
            progress("重采样回 %d Hz（带限 sinc）" % int(sr))
        voc = resample_to(voc, msr, int(sr)).astype(np.float32)
        acc = resample_to(acc, msr, int(sr)).astype(np.float32)
        if drum is not None:
            drum = resample_to(drum, msr, int(sr)).astype(np.float32)

    if voc.shape[0] < n:
        voc = np.concatenate([voc, np.zeros((n - voc.shape[0], 2), np.float32)])
        acc = np.concatenate([acc, np.zeros((n - acc.shape[0], 2), np.float32)])
        if drum is not None:
            drum = np.concatenate([drum, np.zeros((n - drum.shape[0], 2), np.float32)])
    info = {"device": dev, "quality": quality,
            "model": model_name, "model_sr": msr,
            "shifts": preset["shifts"], "overlap": preset["overlap"],
            "segment": seg, "model_segment": mseg}
    if with_drums:
        return (np.ascontiguousarray(voc[:n]), np.ascontiguousarray(acc[:n]),
                None if drum is None else np.ascontiguousarray(drum[:n]), info)
    return (np.ascontiguousarray(voc[:n]), np.ascontiguousarray(acc[:n]), info)


# ---------------------------------------------------------------- RVC

def rvc_scan(model_dir):
    """扫描目录里的 RVC / so-vits-svc 模型。

    返回 [(pth 路径, 同名 .index 或 None, 同名 .json 配置或 None)]
    """
    out = []
    if not model_dir or not os.path.isdir(model_dir):
        return out
    for p in sorted(glob.glob(os.path.join(model_dir, "*.pth"))):
        base = os.path.splitext(p)[0]
        idx = base + ".index"
        cfg = None
        for cand in (base + ".json", os.path.join(model_dir, "config.json")):
            if os.path.isfile(cand):
                cfg = cand
                break
        out.append((p, idx if os.path.isfile(idx) else None, cfg))
    return out


def rvc_status(model_dir):
    """(是否可用, 说明)。

    这里刻意不"假装可用"。RVC 类模型需要三样东西：权重文件、内容编码器
    （HuBERT）、以及能驱动它们的推理代码。前两样用户要自备，第三样我们
    没有可实测的实现 —— 没有真模型就没法验证，宁可不交也不交跑不通的。
    所以状态只有两种：能找到可 import 的推理实现才算可用，否则明确说明
    缺什么。
    """
    models = rvc_scan(model_dir)
    e = probe()
    if not e["torch"][0]:
        return False, "未安装 torch，无法运行任何神经网络后端"
    if not models:
        return False, "模型目录里没有 .pth 文件（%s）" % (model_dir or "未指定")
    drv, dname = _rvc_driver()
    if not drv:
        return False, ("目录里有 %d 个 .pth，但没有可驱动的推理实现。"
                       "本机未检测到 RVC / so-vits-svc 的推理模块。" % len(models))
    return True, "找到 %d 个模型，可用 %s 驱动" % (len(models), dname)


def _rvc_driver():
    """找能跑 RVC 推理的模块。返回 (模块, 名字) 或 (None, "")。"""
    for name in ("rvc.infer.pipeline", "rvc.infer", "so_vits_svc_fork.infer",
                 "inference.infer_tool", "infer"):
        try:
            return __import__(name, fromlist=["*"]), name
        except Exception:
            continue
    return None, ""


# ---------------------------------------------------------------- 主流程

def run(x, sr, cfg, progress=None):
    """跑完整条本地 AI 链路。

    cfg 字典：
        use_demucs : bool   是否用 demucs 做人声分离
        voice      : VOICE_NONE / VOICE_HQ / VOICE_RVC
        semi       : float  变调半音数（仅 VOICE_HQ）
        formant    : float  共振峰保持强度 0..1（仅 VOICE_HQ）
        vocal_gain : float  人声音量
        acc_gain   : float  伴奏音量
        device     : "auto" / "cuda" / "cpu"
        rvc_dir    : str    RVC 模型目录
        rvc_index  : str    选中的 .pth 路径（可选）
        quality    : QUALITY_FAST / QUALITY_STD / QUALITY_HIGH（仅 demucs）

    返回 (立体声 float32 (n,2), info 字典)
    """
    a = np.asarray(x, dtype=np.float32)
    if a.ndim == 1:
        a = np.repeat(a.reshape(-1, 1), 2, axis=1)
    n = a.shape[0]
    info = {"engine": [], "device": ""}

    def rep(s):
        if progress:
            progress(s)

    # ---------- 1. 人声分离 ----------
    if cfg.get("use_demucs"):
        ok, why = demucs_ready()
        if not ok:
            info["warn"] = "demucs 不可用（%s），已跳过人声分离" % why
            vocal = a
            acc = np.zeros_like(a)
            info["engine"].append("人声分离：跳过（%s）" % why)
        else:
            dev = cfg.get("device") or "auto"
            qual = cfg.get("quality") or QUALITY_STD
            vocal, acc, sep = separate(a, sr, device=dev, quality=qual,
                                       progress=rep)
            info["engine"].append(
                "人声分离：demucs %s（%s，%s 档，shifts=%d 重叠=%d%%）"
                % (DEMUCS_MODEL, sep["device"], qual,
                   sep["shifts"], int(round(sep["overlap"] * 100))))
            info["device"] = sep["device"]
            info["separation"] = sep
    else:
        vocal = a
        acc = np.zeros_like(a)
        info["engine"].append("人声分离：未启用")

    if cfg.get("keep_stems"):
        info["stems"] = (vocal, acc)

    # ---------- 2. 人声处理 ----------
    voice = cfg.get("voice", VOICE_HQ)
    semi = float(cfg.get("semi", 0.0))
    formant = float(cfg.get("formant", 1.0))

    def _proc_voice(sig):
        """对一条单声道人声施加所选的处理。"""
        if voice == VOICE_HQ:
            return hq_voice.process(sig, sr, hq_voice.semitone_to_ratio(semi),
                                    formant=formant)
        return _run_rvc(sig, sr, cfg, rep)

    if voice == VOICE_HQ:
        if abs(semi) > 1e-9:
            rep("高质量变调 %+.1f 半音…" % semi)
        info["engine"].append("人声处理：内置高质量变调 %+.1f 半音，共振峰保持 %d%%"
                              % (semi, int(round(formant * 100))))
        info["f0_ratio"] = float(hq_voice.semitone_to_ratio(semi))
    elif voice == VOICE_RVC:
        ok, why = rvc_status(cfg.get("rvc_dir"))
        if not ok:
            raise RuntimeError("RVC 后端不可用：%s。请改用「内置高质量变调」。"
                               % why)
        rep("RVC 音色转换…")
        info["engine"].append("人声处理：RVC 音色转换")
    else:
        info["engine"].append("人声处理：不处理")

    if voice == VOICE_NONE:
        # ★ 不处理就必须原样通过。早期版本在这里把它压成单声道
        #   （vm = 0.5*(L+R)），用户选"只分离 / 只调音量、不变调"时立体声
        #   就被悄悄弄丢了 —— 实测最大偏差 0.127，这是有损的，不是"没变"。
        vres = vocal.astype(np.float64)
    else:
        separated = bool(cfg.get("use_demucs")) and \
            not any("跳过" in s for s in info["engine"])
        if separated:
            # demucs 的人声源两路高度相关，取中置再处理，避免两路各跑一遍
            # 还引入相位差
            m = _proc_voice(0.5 * (vocal[:, 0] + vocal[:, 1]).astype(np.float64))
            m = np.asarray(m, dtype=np.float64).reshape(-1)
            vres = np.repeat(m.reshape(-1, 1), 2, axis=1)
        else:
            # 没做分离时"人声"其实是整条混音；逐声道独立处理，保住立体声
            cols = []
            for c in (0, 1):
                if c:
                    rep("处理右声道…")
                cols.append(np.asarray(
                    _proc_voice(np.ascontiguousarray(vocal[:, c]).astype(np.float64)),
                    dtype=np.float64).reshape(-1))
            vres = np.stack(cols, axis=1)

    vres = np.asarray(vres, dtype=np.float64)
    if vres.ndim == 1:
        vres = np.repeat(vres.reshape(-1, 1), 2, axis=1)
    if vres.shape[0] < n:
        vres = np.concatenate([vres, np.zeros((n - vres.shape[0], vres.shape[1]))])
    vres = vres[:n]

    # ---------- 3. 重新混合 ----------
    vg = float(cfg.get("vocal_gain", 1.0))
    ag = float(cfg.get("acc_gain", 1.0))
    accd = np.asarray(acc, dtype=np.float64)
    if accd.shape[0] < n:
        accd = np.concatenate([accd, np.zeros((n - accd.shape[0], 2))])
    accd = accd[:n]
    if cfg.get("route_stems"):
        # 左 = 人声、右 = 伴奏。文件模式的两个音轨都是单声道内容，
        # 各占一个物理声道 —— 这是"分离"模块现在的输出语义。
        y = np.empty((n, 2), dtype=np.float64)
        y[:, 0] = 0.5 * (vres[:, 0] + vres[:, 1]) * vg
        y[:, 1] = 0.5 * (accd[:, 0] + accd[:, 1]) * ag
        info["engine"].append("输出：左声道=人声 %.2f× / 右声道=伴奏 %.2f×"
                              % (vg, ag))
    else:
        y = vres * vg + accd * ag
        info["engine"].append("重混：人声 %.2f× / 伴奏 %.2f×" % (vg, ag))

    peak = float(np.max(np.abs(y))) if n else 0.0
    if peak > 1.0:
        y = y / peak
        info["warn"] = "峰值 %.2f 已归一化到 1.0，避免削波" % peak
    return y.astype(np.float32), info


def _run_rvc(vm, sr, cfg, rep):
    """调用外部 RVC 推理实现。

    本机没有可实测的 RVC 模型，所以这里只做"把活派发给已安装的推理模块"
    这一件事，任何一步出错都会抛出带明确原因的异常，不会静默返回错的
    结果。真正的转换逻辑在外部仓库里，不在本模块里 —— 没有模型可验证
    的实现不写进来。
    """
    mod, name = _rvc_driver()
    if mod is None:
        raise RuntimeError("未找到可用的 RVC 推理模块")
    raise RuntimeError(
        "已检测到推理模块 %s，但本模块没有实现到它的适配层（需要针对具体的 "
        "pipeline 签名对接，没有真实模型无法验证）。请改用「内置高质量变调」，"
        "或告知具体的 RVC 仓库版本以便对接。" % name)
