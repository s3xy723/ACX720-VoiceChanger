# -*- coding: utf-8 -*-
"""
audio_io.py  ——  文件读写与声卡设备枚举

对外统一的数据格式：float32，范围 [-1, 1)，形状 (n,) 单声道 或 (n, c) 多声道。
这样 DSP 内核（int16 定点）和声卡之间的转换都集中在这一个文件里。

多格式支持（2026-10-13 修）
---------------------------
按下面这个顺序尝试，谁先成功用谁，**每个后端各自的报错都会收集起来**：

    stdlib wave  —— 只认 PCM WAV，零依赖、最快，.wav 优先走它
    soundfile    —— libsndfile：WAV/FLAC/OGG/MP3/AIFF/AU/W64…（推荐装）
    PyAV (av)    —— 内置 ffmpeg：连 M4A/AAC/WMA/OPUS 都能解，格式最全
    librosa      —— 最后兜底

以前这里有两个坑，导致实际只能用 WAV：
  1) PyAV 分支写的是 `frame.to_ndarray(format="fltp")`，而 PyAV 13 之后
     这个关键字参数被删掉了，于是**每一种格式都抛 TypeError**；
  2) 异常处理把 librosa 的报错当成最终原因抛出去（"No module named
     'lazy_loader'…"），完全掩盖了真正失败的 PyAV。
现在 PyAV 走 `AudioResampler(format="fltp")` 统一成平面 float32，
不再依赖那个会随版本变的签名；报错也会把每个后端的原因列全。
"""

import os
import struct
import wave

import numpy as np


def _wav_format_tag(path):
    """读 WAV 的 fmt chunk 里的 wFormatTag。1=PCM, 3=IEEE_FLOAT, 0xFFFE=可扩展。

    为什么需要它：判断"这是 int32 还是 float32"不能靠 try/except —— 见
    _read_wav_std 里 sw==4 的说明。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                cid = hdr[0:4]
                sz = struct.unpack("<I", hdr[4:8])[0]
                if cid == b"fmt ":
                    tag = struct.unpack("<H", f.read(2))[0]
                    if tag == 0xFFFE:
                        # WAVE_FORMAT_EXTENSIBLE：真正的格式在 subformat 里，
                        # 位于 cbSize(2) + validBits(2) + channelMask(4) 之后
                        f.read(8)
                        tag = struct.unpack("<H", f.read(2))[0]
                    return tag
                f.seek(sz + (sz & 1), 1)
    except Exception:
        return None

# ---------------------------------------------------------------- 格式表

#: 扩展名 -> (soundfile 格式名, 默认子类型)
SF_FORMATS = {
    ".wav": ("WAV", "PCM_16"),
    ".wave": ("WAV", "PCM_16"),
    ".flac": ("FLAC", "PCM_16"),
    ".ogg": ("OGG", "VORBIS"),
    ".oga": ("OGG", "VORBIS"),
    ".opus": ("OGG", "OPUS"),
    ".aiff": ("AIFF", "PCM_16"),
    ".aif": ("AIFF", "PCM_16"),
    ".au": ("AU", "PCM_16"),
    ".w64": ("W64", "PCM_16"),
    ".caf": ("CAF", "PCM_16"),
}

#: 可以直接写出去的扩展名（其余一律回退成 WAV）
WRITABLE = set(SF_FORMATS)

#: 文件选择框用的通配串
AUDIO_GLOBS = ("*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.oga *.opus *.aiff *.aif "
               "*.wma *.au *.w64 *.caf *.mp4")


class AudioReadError(RuntimeError):
    """读文件失败，errs 里是每个后端各自的原因。"""

    def __init__(self, path, errs):
        self.path = path
        self.errs = list(errs)
        detail = "；".join("%s: %s" % (n, e) for n, e in self.errs) or "没有可用后端"
        super(AudioReadError, self).__init__(
            "无法读取音频文件 %s\n  %s\n"
            "（装了 soundfile 或 av 就能读 FLAC/MP3/OGG/M4A；"
            "只装 numpy 时只能读 PCM WAV）"
            % (os.path.basename(str(path)), detail))


def _have(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def backends():
    """当前可用的解码后端，给界面/自检展示用。"""
    out = [("wave", True)]
    for n in ("soundfile", "av", "librosa"):
        out.append((n, _have(n)))
    return out


# ---------------------------------------------------------------- 设备

def list_devices(kind="input"):
    """返回 [(index, 名称, hostapi 名), ...]。kind: input / output"""
    try:
        import sounddevice as sd
    except Exception:
        return []
    out = []
    try:
        devs = sd.query_devices()
        hosts = sd.query_hostapis()
    except Exception:
        return []
    for i, d in enumerate(devs):
        cap = d.get("max_input_channels" if kind == "input" else "max_output_channels", 0)
        if cap and cap > 0:
            hidx = d.get("hostapi", 0)
            hname = hosts[hidx]["name"] if hidx < len(hosts) else "?"
            out.append((i, d.get("name", "?"), hname, int(cap)))
    return out


def probe_stream(kind, index, samplerate=None, channels=2):
    """真的开一条流再立刻关掉，判断这个「设备 + 采样率」组合能不能用。

    为什么不用 sd.check_output_settings()
    -------------------------------------
    它对 Windows 的 **WDM-KS** 条目一律返回 OK，但真去开必失败：
        Error opening OutputStream: Unanticipated host error [PaErrorCode -9999]
    在部分驱动/语言环境下这条就显示成用户看到的 "permission denied"。
    实测本机 17 个输出设备里 6 个是这种（名字还很正经，比如
    "Speakers 1 (Realtek HD Audio output with HAP)"），选了必然报错。

    只有真开一次才是权威判据。这里只 open 不 start，所以不会出声、
    也不会抢走设备（开完马上 close）。
    """
    try:
        import sounddevice as sd
    except Exception:
        return False
    ch = int(channels) if channels else 2
    kw = dict(device=int(index), channels=ch, dtype="float32")
    if samplerate:
        kw["samplerate"] = int(samplerate)
    try:
        st = (sd.OutputStream(**kw) if kind == "output"
              else sd.InputStream(**kw))
        st.close()
        return True
    except Exception:
        return False


def usable_devices(devs, kind, samplerate=None, channels=2):
    """把 list_devices() 的结果过一遍可打开性筛选。

    返回 (kept, dropped)；dropped 是 [(index, 名称, hostapi), ...]，
    调用方把它打进日志 —— 用户看不到某个设备时得知道是被筛掉了、为什么。
    """
    kept, dropped = [], []
    for d in devs:
        if probe_stream(kind, d[0], samplerate, channels):
            kept.append(d)
        else:
            dropped.append((d[0], d[1], d[2]))
    return kept, dropped


def default_devices():
    try:
        import sounddevice as sd
        return sd.default.device
    except Exception:
        return (None, None)


# ---------------------------------------------------------------- 读文件

def _read_wav_std(path):
    """stdlib wave，支持 8/16/24/32 位 PCM。返回 (float32, sr) 或 None。"""
    try:
        wf = wave.open(path, "rb")
    except Exception:
        return None
    try:
        # 压缩过的 WAV（ADPCM / µ-law / A-law）stdlib wave 会给出"能读"的
        # 假象，实际解出来是噪声。宁可返回 None 让 soundfile/PyAV 去解。
        if wf.getcomptype() != "NONE":
            wf.close()
            return None
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    except Exception:
        wf.close()
        return None
    wf.close()

    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 4:
        # ★ 32 位有 int32 和 float32 两种，必须看 fmt chunk 的 wFormatTag。
        # 不能照原样"先按 int32 试、float32 靠 except 兜"：np.frombuffer
        # 把 IEEE754 位型当 int32 读**不会抛异常**，只会得到 1e-8 量级的
        # 伪随机噪声。结果是文件"读成功了"、波形看着也有信号，但后面所有
        # DSP、频谱、导出结果全错，而且极难定位到是读文件这一步。
        if _wav_format_tag(path) == 3:
            a = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        else:
            a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) |
             (b[:, 1].astype(np.int32) << 8) |
             (b[:, 2].astype(np.int32) << 16))
        v = np.where(v >= (1 << 23), v - (1 << 24), v)
        a = v.astype(np.float32) / 8388608.0
    else:
        return None

    a = a.reshape(-1, nch)
    return a.astype(np.float32), sr


def _mono_or_multi(a):
    """把 (n,c) 的单声道结果压回 (n,)，其余原样。"""
    a = np.asarray(a, dtype=np.float32)
    if a.ndim == 2 and a.shape[1] == 1:
        return a[:, 0]
    return a


def _read_soundfile(path):
    import soundfile as sf
    a, sr = sf.read(path, always_2d=True, dtype="float32")
    return _mono_or_multi(np.asarray(a, dtype=np.float32)), int(sr)


def _pyav_frame_to_float(frame):
    """把 AudioFrame 转成 (n, c) float32，兼容新旧 PyAV。

    新 PyAV 的 to_ndarray() 不带 format 参数，返回"原生"排布：
        平面格式 (fltp/s16p…) -> (channels, samples)
        打包格式 (flt/s16…)   -> (1, samples*channels)
    这里两种都接住，并按位宽归一化到 [-1,1)。
    """
    try:                                   # PyAV <= 12 的老写法，能成就最省事
        return np.asarray(frame.to_ndarray(format="fltp"), dtype=np.float32)
    except TypeError:
        pass
    a = frame.to_ndarray()
    ch = int(frame.layout.nb_channels) or 1
    name = (frame.format.name or "").lower()
    if not name.endswith("p"):             # 打包 -> 拆成交错
        a = a.reshape(-1, ch).T
    if a.ndim == 1:
        a = a.reshape(1, -1)
    a = np.asarray(a)
    if a.dtype == np.float32 or a.dtype == np.float64:
        return a.astype(np.float32)
    if a.dtype == np.int16:
        return a.astype(np.float32) / 32768.0
    if a.dtype == np.int32:
        return a.astype(np.float32) / 2147483648.0
    if a.dtype == np.uint8:
        return (a.astype(np.float32) - 128.0) / 128.0
    return a.astype(np.float32)


def _read_pyav(path):
    """PyAV 解码 —— 覆盖 M4A/AAC/WMA/OPUS 这些 libsndfile 不一定支持的格式。

    统一先过一遍 AudioResampler(format="fltp")：不管源是 s16 还是 fltp、
    平面还是打包，出来的都是 (channels, samples) 的 float32，逐点保真
    （同速率只做格式转换，不重采样）。
    """
    import av
    with av.open(str(path)) as cont:
        if not cont.streams.audio:
            raise RuntimeError("文件里没有音频流")
        st = cont.streams.audio[0]
        sr = int(st.codec_context.sample_rate or getattr(st, "rate", 0) or 0)
        res = None
        if sr > 0:
            try:
                layout = st.layout.name if st.layout is not None else "stereo"
                res = av.AudioResampler(format="fltp", layout=layout, rate=sr)
            except Exception:
                res = None
        chunks = []
        for frame in cont.decode(audio=0):
            if res is not None:
                for of in res.resample(frame):
                    chunks.append(np.asarray(of.to_ndarray(), dtype=np.float32))
            else:
                chunks.append(_pyav_frame_to_float(frame))
        if res is not None:
            for of in res.resample(None):
                chunks.append(np.asarray(of.to_ndarray(), dtype=np.float32))
        if not chunks:
            raise RuntimeError("PyAV 没解出任何音频帧")
        a = np.concatenate(chunks, axis=1).T.astype(np.float32)
        if not sr:
            sr = int(st.codec_context.sample_rate or 48000)
    return _mono_or_multi(a), sr


def _read_librosa(path):
    import librosa
    a, sr = librosa.load(str(path), sr=None, mono=False)
    a = np.asarray(a, dtype=np.float32)
    if a.ndim == 2:
        a = a.T
    return _mono_or_multi(a), int(sr)


def load_audio(path, info=None):
    """读任意常见音频 -> (float32 array (n,) 或 (n,c), 采样率)。

    info 传一个 dict 时，会填入实际生效的后端和扩展名，供界面显示。
    全部后端都失败时抛 AudioReadError，消息里列出每个后端各自的报错。
    """
    ext = os.path.splitext(str(path))[1].lower()
    errs = []

    # 只认 PCM WAV 的一条快路，零依赖
    if ext in (".wav", ".wave", ""):
        try:
            r = _read_wav_std(path)
        except Exception as e:
            errs.append(("wave", e))
        else:
            if r is not None:
                a, sr = r
                _fill(info, "wave", ext)
                return _mono_or_multi(a), sr
            errs.append(("wave", "不是标准 PCM WAV"))

    # 其余交给三个库；soundfile 快且覆盖 wav/flac/ogg/mp3/aiff
    for name, fn in (("soundfile", _read_soundfile),
                     ("PyAV", _read_pyav),
                     ("librosa", _read_librosa)):
        try:
            a, sr = fn(path)
        except Exception as e:
            errs.append((name, e))
            continue
        _fill(info, name, ext)
        return a, sr

    raise AudioReadError(path, errs)


def _fill(info, backend, ext):
    if info is not None:
        info["backend"] = backend
        info["ext"] = ext


# ---------------------------------------------------------------- 写文件

def norm_sample_rate(sr):
    """采样率统一成**正整数**，不合法就直接抛 ValueError。

    ★ 为什么必须在写文件这一层再收一次口：
      * libsndfile 拿到 0 会抛 `Error: sampling rate not specified`，
        它**不是 OSError** —— 上层的 _save_audio_retry 只兜 OSError，
        于是这条错误会直接冒出去变成"输出文件报错"，且完全看不出是采样率的事。
      * 24414.0625 这种小数（FPGA 的 50MHz/2048）拿去开 PortAudio 也会被拒，
        而 sf.write 会悄悄写成 24414 —— 于是**文件里写的和界面上显示的不一致**，
        后面再读回来就对不上了。
      这里一次性收敛，写入/返回/界面显示的都只能是同一个 int。
    """
    try:
        v = int(round(float(sr)))
    except Exception:
        raise ValueError("采样率无效：%r" % (sr,))
    if v <= 0:
        raise ValueError("采样率必须为正整数，实际是 %r" % (sr,))
    return v


def sanitize_audio(data):
    """去掉 NaN / Inf，并夹回 [-1, 1)。

    NaN 一旦进了 libsndfile，要么写盘报错、要么写出一段巨响的垃圾 ——
    而且这类错误同样不是 OSError，重试逻辑兜不住。宁可在这里替成静音。
    """
    a = np.asarray(data)
    if a.dtype == np.int16 or a.dtype == np.int32 or a.dtype == np.uint8:
        return a
    a = np.asarray(a, dtype=np.float32)
    if not np.all(np.isfinite(a)):
        a = np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(a, -1.0, 1.0).astype(np.float32)


def ensure_dir(path):
    """保证目标目录存在；建不出来就抛（比 libsndfile 的 FileNotFoundError 清楚）。"""
    d = os.path.dirname(os.path.abspath(str(path)))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    return path


def _as_unit_float(data):
    """写出前统一成 [-1,1) 的 float32。

    这里必须容错：调用方（比如分性别音轨导出）手上拿到的往往是 int16，
    如果直接按 float 写出去，±10000 会被 clip 成满量程方波 —— 文件能生成、
    也不报错，但内容是彻底错的。宁可在这里多认几种整型。
    """
    a = np.asarray(data)
    if a.dtype == np.int16:
        return a.astype(np.float32) / 32768.0
    if a.dtype == np.int32:
        return a.astype(np.float32) / 2147483648.0
    if a.dtype == np.uint8:
        return (a.astype(np.float32) - 128.0) / 128.0
    return np.asarray(a, dtype=np.float32)


def save_wav(path, data, sr):
    """data: float32 [-1,1) 或 int16/in32，形状 (n,) / (n,c)。写成 16 位 PCM WAV。

    采样率/声道/位深在这里**显式定死**：
        采样率 = norm_sample_rate(sr) 的正整数
        声道数 = 数据的实际列数（1 = 单声道，2 = 立体声），不再猜
        位深   = 16 bit PCM（<i2），与 SF_FORMATS 里 .wav 的 PCM_16 一致
    """
    sr = norm_sample_rate(sr)
    a = sanitize_audio(_as_unit_float(data))
    if a.ndim == 1:
        a = a.reshape(-1, 1)
    nch = int(a.shape[1])
    pcm = np.clip(np.round(a * 32767.0), -32768, 32767).astype("<i2")
    ensure_dir(path)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return str(path)


def save_audio(path, data, sr, subtype=None):
    """按扩展名写文件：.wav/.flac/.ogg/.opus/.aiff/.au/.w64/.caf。

    data 可以是 [-1,1) 的 float，也可以是 int16/int32（会自动换算）。
    有 soundfile 就用它（FLAC 无损、OGG/OPUS 有损）；没有 soundfile 时
    只有 .wav 能写（走 stdlib wave），其余格式回退成同名 .wav 并在返回值
    里给出真实路径。返回写出的路径。
    """
    sr = norm_sample_rate(sr)          # 先定死，后面所有分支都用这同一个值
    a = sanitize_audio(_as_unit_float(data))
    if a.ndim == 1:
        a = a.reshape(-1, 1)
    ext = os.path.splitext(str(path))[1].lower()
    fmt, sub = SF_FORMATS.get(ext, (None, None))
    if subtype is not None:
        sub = subtype

    if fmt is not None and _have("soundfile"):
        try:
            import soundfile as sf
            ensure_dir(path)
            sf.write(str(path), a, sr, format=fmt, subtype=sub)
            return str(path)
        except Exception:
            if ext in (".wav", ".wave"):       # 子类型不被支持时退到 stdlib
                pass
            else:
                raise

    if fmt is None and ext not in (".wav", ".wave"):
        # 不认识的扩展名：**不要**把 WAV 数据塞进 .xyz 里骗人，
        # 改成同名 .wav 写出去，并把真实路径返回给调用方。
        path = os.path.splitext(str(path))[0] + ".wav"
    return save_wav(path, a, sr)


# ---------------------------------------------------------------- 工具

def to_int16(x):
    """float32 -> int16 ndarray"""
    a = np.asarray(x, dtype=np.float32)
    return np.clip(np.round(a * 32767.0), -32768, 32767).astype(np.int16)


def to_float(x):
    """int16 ndarray -> float32"""
    return np.asarray(x, dtype=np.int16).astype(np.float32) / 32768.0


def resample(x, sr_in, sr_out):
    """线性插值重采样。x: (n,) 或 (n,c) float32。"""
    if sr_in == sr_out:
        return x
    a = np.asarray(x, dtype=np.float32)
    one_d = (a.ndim == 1)
    if one_d:
        a = a.reshape(-1, 1)
    n_in = a.shape[0]
    n_out = int(round(n_in * float(sr_out) / float(sr_in)))
    if n_out < 1:
        n_out = 1
    pos = np.linspace(0.0, n_in - 1.0, n_out).astype(np.float64)
    i0 = np.floor(pos).astype(np.int64)
    i1 = np.minimum(i0 + 1, n_in - 1)
    frac = (pos - i0).astype(np.float32).reshape(-1, 1)
    out = a[i0] * (1.0 - frac) + a[i1] * frac
    out = out.astype(np.float32)
    return out[:, 0] if one_d else out


def fit_channels(x, channels):
    """把 (n,) 或 (n,c) 调整到目标声道数：不足复制，多余降混。"""
    a = np.asarray(x, dtype=np.float32)
    if channels == 1:
        if a.ndim == 1:
            return a
        return a.mean(axis=1)
    if a.ndim == 1:
        return np.repeat(a.reshape(-1, 1), channels, axis=1)
    c = a.shape[1]
    if c == channels:
        return a
    if c > channels:
        return a[:, :channels]
    return np.concatenate([a, np.repeat(a[:, -1:], channels - c, axis=1)], axis=1)


def to_stereo(x):
    """强制成 (n, 2)：单声道复制到左右两路，多余声道只取前两路。

    整个上位机对外的统一约定 —— 输入和输出一律双声道，
    每条声道各跑一条独立的效果链（见 dsp_core.StereoChain）。
    """
    a = np.asarray(x, dtype=np.float32)
    if a.ndim == 1:
        return np.repeat(a.reshape(-1, 1), 2, axis=1)
    if a.shape[1] == 2:
        return a
    if a.shape[1] == 1:
        return np.repeat(a, 2, axis=1)
    if a.shape[1] > 2:
        return a[:, :2]
    return np.zeros((a.shape[0], 2), dtype=np.float32)


def duration_str(n, sr):
    s = float(n) / float(sr) if sr else 0.0
    return "%d:%06.3f  (%d 采样点)" % (int(s // 60), s % 60, n)
