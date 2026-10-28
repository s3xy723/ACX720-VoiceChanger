# -*- coding: utf-8 -*-
"""player.py —— 内嵌音频播放器（带左右声道开关）

为什么不用 `sounddevice.play()`：
    `sd.play()` 是一次性把整段丢给 PortAudio，播放过程中改不了任何东西。
    这里需要"边放边关左/右声道"、拖动进度、暂停后续播，所以自己开一个
    `OutputStream`，在音频回调里按当前增益从缓冲区取样本。

接口
----
    Player              纯逻辑：load / play / pause / stop / seek / 声道开关
    PlayerBar           tkinter 控件条：播放、停止、L/R、进度、音量、时间
"""

import threading
import time

import numpy as np

import theme

try:
    import sounddevice as sd
except Exception:
    sd = None


def _fmt_time(sec):
    sec = max(0.0, float(sec))
    m = int(sec // 60)
    s = sec - m * 60
    return "%02d:%04.1f" % (m, s)


def _dev_desc(device, sr=None):
    """把「设备索引 + 采样率」翻译成人能看懂的一句话。

    报错日志里必须有这个：只打 PaErrorCode 的话，用户完全不知道是哪张
    卡、哪个接口、哪个采样率被拒了，只能归结为"有程序占用"。
    """
    if device is None:
        return "系统默认输出设备" + (" @ %d Hz" % sr if sr else "")
    name, host = "?", "?"
    try:
        d = sd.query_devices(device)
        name = d.get("name", "?")
        host = sd.query_hostapis(d.get("hostapi", 0))["name"]
    except Exception:
        pass
    try:
        native = int(sd.query_devices(device).get("default_samplerate", 0) or 0)
    except Exception:
        native = 0
    txt = "dev=%d %s [%s]" % (device, name, host)
    if sr:
        txt += " @ %d Hz" % sr
        if native and int(sr) != native:
            txt += "（该设备原生 %d Hz，不匹配会直接拒）" % native
    return txt


class Player(object):
    """双声道播放器：数据常驻内存，播放位置可随时跳转。

    声道开关是在**输出端**做的（左增益/右增益），不改缓冲区，
    所以关掉左声道再打开，右声道不会重新开始，也不会断音。
    """

    def __init__(self, log=None):
        self.log = log or (lambda s: None)
        self.data = None
        self.sr = 48000
        self.pos = 0
        self.playing = False
        self.volume = 0.8
        self.gain = [1.0, 1.0]
        self.device = None        # None = 跟随系统默认输出；否则是 PortAudio 设备索引
        self._stream = None
        self._lock = threading.Lock()
        self._tag = ""
        self._ended = False
        # ---- 爆音排查埋点：underrun/overrun 计数 ----
        # 连续爆音的第一嫌疑就是回调被拖慢导致 underrun（设备要数据时不给，
        # 驱动填静音 → "咔"）。没有计数就只能靠耳朵猜。
        self.underruns = 0
        self.overruns = 0
        self._cb_calls = 0
        self._last_status_log = 0.0

    # ------------------------------------------------------------ 状态
    @property
    def available(self):
        return sd is not None

    @property
    def duration(self):
        if self.data is None:
            return 0.0
        return self.data.shape[0] / float(self.sr)

    @property
    def position(self):
        return min(self.pos / float(self.sr), self.duration)

    def has_data(self):
        return self.data is not None and self.data.shape[0] > 0

    def set_channel(self, ch, on):
        with self._lock:
            self.gain[int(ch)] = 1.0 if on else 0.0

    def channel_on(self, ch):
        return self.gain[int(ch)] > 0.5

    def set_volume(self, v):
        with self._lock:
            self.volume = float(np.clip(v, 0.0, 1.5))

    # ------------------------------------------------------------ 输出设备
    def set_device(self, dev):
        """切换输出设备。None = 跟随系统默认。

        PortAudio 的流是**绑定设备**的，改 device 必须把流关掉重开。
        正在播的时候切设备：位置保留、用新设备接着放 —— 用户只是想换个
        口听同一段，不应该回到开头。
        """
        dev = None if dev is None else int(dev)
        if dev == self.device:
            return
        was = self.playing
        self._close_stream()
        self.playing = False
        self._ended = False
        self.device = dev
        if was and self.has_data():
            self.play()

    def _fit_rate(self):
        """把缓冲区的采样率对齐到**设备真正接受**的那个。

        ★ 这是文件变声"打开就报 permission denied"的头号原因：
          Player 是用 `self.sr`（= **文件自己的**采样率）去开 OutputStream 的。
          而 WASAPI 设备往往只认自己的原生采样率 —— 本机实测 Realtek 和
          VB-Cable 都只吃 48 kHz，拿 44.1 kHz（也就是绝大多数音乐文件）去开，
          PortAudio 直接回 `Invalid sample rate`；在不同驱动/语言环境下这条
          会被报成各种样子，用户统统归结为"permission denied"。

          MME / DirectSound 那一批（dev 4/11）倒是 44.1k 也收，所以这个
          问题表现为"换一张卡就好了"，很难往采样率上想。

        只在设备确实不支持时才重采样，且只做一次（不是每帧），
        音质损失是线性插值级别的，比"根本放不出来"强得多。
        """
        if self.data is None or sd is None:
            return
        dev = self.device
        if dev is None:
            try:
                dev = sd.query_devices(kind="output")
                dev = dev.get("index", None) if isinstance(dev, dict) else None
            except Exception:
                dev = None
        if dev is None:
            return
        try:
            sd.check_output_settings(device=dev, channels=2,
                                     dtype="float32", samplerate=self.sr)
            return                                  # 设备吃这个率，不用动
        except Exception:
            pass
        try:
            native = int(sd.query_devices(dev).get("default_samplerate", 0) or 0)
        except Exception:
            native = 0
        if not native or native == self.sr:
            return
        try:
            import audio_io
            with self._lock:
                old = self.sr
                self.data = audio_io.resample(self.data, old, native)
                pos_sec = self.pos / float(old)
                self.sr = native
                self.pos = max(0, min(int(pos_sec * native),
                                      self.data.shape[0] - 1))
        except Exception as e:
            self.log("重采样失败，按原采样率硬开：%r" % (e,))
            return
        self.log("播放器重采样 %d Hz → %d Hz（该设备只支持后者）" % (old, native))

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # ------------------------------------------------------------ 装载
    def load(self, data, sr, tag=""):
        self.stop()
        a = np.asarray(data)
        # ★ 整型输入必须先做量程换算，不能只改 dtype。
        # np.asarray(int16 数组, dtype=float32) 得到的是 20000.0 而不是 0.61，
        # 后面那句 clip 会把几乎每个样本推到 ±1 —— 输出直接变成满量程方波。
        if np.issubdtype(a.dtype, np.integer):
            info = np.iinfo(a.dtype)
            a = a.astype(np.float32) / float(max(abs(info.min), info.max))
        else:
            a = a.astype(np.float32)
        if a.ndim == 1:
            a = np.repeat(a.reshape(-1, 1), 2, axis=1)
        elif a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        elif a.shape[1] > 2:
            a = a[:, :2]
        a = np.ascontiguousarray(a)
        # 只在真的越界时才复制；正常 float32 音频直接引用原数组，
        # 这样 proc_data / file_data 与播放器共享同一块内存（缓存逻辑
        # 可以用 `player.data is proc_data` 判断"这份数据还在用"）。
        if a.size and (float(a.min()) < -1.0 or float(a.max()) > 1.0):
            a = np.clip(a, -1.0, 1.0)
        new_sr = int(sr) if sr else 48000
        if self._stream is not None and new_sr != self.sr:
            # 采样率变了必须重开流，PortAudio 的流是绑定采样率的
            self._close_stream()
        # data/sr/pos 必须整组在锁内换：回调线程正拿着 self.data[self.pos:]
        # 切片，若 GUI 线程先改 data 再改 pos，会读到"新数组的旧位置"。
        with self._lock:
            self.data = a
            self.sr = new_sr
            self.pos = 0
            self._tag = tag
            self._ended = False
        self.log("播放器载入：%s，%.1f 秒，%d Hz" %
                 (tag or "音频", self.duration, self.sr))

    def unload(self):
        self.stop()
        with self._lock:
            self.data = None
            self.pos = 0
        self._tag = ""

    # ------------------------------------------------------------ 传输
    def play(self):
        if not self.has_data():
            return False
        if not self.available:
            self.log("播放器不可用：没有 sounddevice")
            return False
        if self.pos >= self.data.shape[0] - 1:
            self.pos = 0
        if self._ended:
            # 上一段播完后流已经 CallbackStop，重新开一条干净的流
            try:
                if self._stream is not None:
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._ended = False
        try:
            if self._stream is None:
                # 开流之前先把采样率对齐到设备能接受的那个，不然 44.1 kHz
                # 的文件在 WASAPI 卡上直接 "Invalid sample rate"（见 _fit_rate）
                self._fit_rate()
                self._stream = sd.OutputStream(
                    samplerate=self.sr, channels=2, dtype="float32",
                    blocksize=1024, latency="low",
                    device=self.device,
                    callback=self._cb, finished_callback=self._finished)
            if not self._stream.active:
                self._stream.start()
            self.playing = True
            self.log("播放：%s  → %s"
                     % (self._tag or "音频", _dev_desc(self.device, self.sr)))
            return True
        except Exception as e:
            self.playing = False
            # ★ 只打 %r 看不出是谁拒的。"permission denied" 类报错必须把
            #   设备名 / 接口 / 采样率一起打出来，否则用户只能猜"谁占用了"。
            self.log("播放启动失败：%r\n  目标：%s"
                     % (e, _dev_desc(self.device, self.sr)))
            return False

    def pause(self):
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
        self.playing = False

    def stop(self):
        self.pause()
        with self._lock:
            self.pos = 0
        self.playing = False
        self._ended = False

    def seek(self, sec):
        if not self.has_data():
            return
        # 钳位和赋值必须在同一把锁里一次做完：原来先写一次、锁外又改一次，
        # 中间那瞬间的 pos 是越界值，回调线程读到了就会取到空切片。
        with self._lock:
            n = int(np.clip(float(sec), 0.0, self.duration) * self.sr)
            self.pos = max(0, min(n, self.data.shape[0] - 1))
        self._ended = False

    def close(self):
        self.pause()
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        with self._lock:
            self.data = None
        self.playing = False

    def __del__(self):
        # PortAudio 的流没终止时进程退出会挂住（"退出卡死"的常见成因）。
        # 正常流程走 _on_close，这里只是给异常路径兜底。
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 音频回调
    def _finished(self):
        self.playing = False

    def _cb(self, outdata, frames, time_info, status):
        # ★ 埋点：status 非空就说明这一帧驱动没按时拿到/送走数据。
        #   只在累计到一定数量或首次出现时打日志，避免刷屏把日志冲垮。
        if status:
            s = str(status)
            if "underflow" in s or "underrun" in s:
                self.underruns += 1
            elif "overflow" in s or "overrun" in s:
                self.overruns += 1
            now = time.time()
            tot = self.underruns + self.overruns
            if tot in (1, 10, 50) or (tot and tot % 200 == 0):
                if now - self._last_status_log > 0.5:
                    self._last_status_log = now
                    self.log("音频回调异常：%s（累计欠载 %d / 过载 %d）→ 会出现爆音"
                             % (s.strip(), self.underruns, self.overruns))
        self._cb_calls += 1
        outdata.fill(0.0)
        with self._lock:
            if self.data is None:
                raise sd.CallbackStop
            n_avail = self.data.shape[0] - self.pos
            n = min(frames, max(0, n_avail))
            if n > 0:
                vol = self.volume
                blk = self.data[self.pos:self.pos + n]
                # 音量上限是 1.5，gain × vol 可以超过 ±1，交付设备时会硬削波。
                # 这里显式限幅，和实时链路（_out_cb 对 ingain 的处理）保持一致。
                outdata[:n, 0] = np.clip(blk[:, 0] * self.gain[0] * vol, -1.0, 1.0)
                outdata[:n, 1] = np.clip(blk[:, 1] * self.gain[1] * vol, -1.0, 1.0)
                self.pos += n
            ended = n < frames
        if ended:
            self.playing = False
            self._ended = True
            raise sd.CallbackStop


# tkinter 放到后面 import：player.py 的纯逻辑部分可以被 headless 测试直接导入。
import tkinter as tk                      # noqa: E402
from tkinter import ttk                   # noqa: E402


class PlayerBar(ttk.Frame):
    """一行式播放器控件：▶/⏸、■、左/右声道、进度、时间、音量。

    设计成一行是为了不把文件页撑高 —— 这一页已经塞了输入/输出、
    导出选项、处理按钮、进度条和状态文字，再加一张卡片会把
    Notebook 压到看不见按钮。
    """

    def __init__(self, master, player, log=None, **kw):
        kw.setdefault("padding", (0, 2))
        ttk.Frame.__init__(self, master, **kw)
        self.player = player
        self.log = log or (lambda s: None)
        self._drag = False

        self.btn_play = ttk.Button(self, text="▶ 播放", width=8,
                                   style="Tool.TButton", command=self._toggle)
        self.btn_play.pack(side="left")
        self.btn_stop = ttk.Button(self, text="■", width=3,
                                   style="Tool.TButton", command=self._stop)
        self.btn_stop.pack(side="left", padx=(3, 8))

        self.v_l = tk.BooleanVar(value=True)
        self.v_r = tk.BooleanVar(value=True)
        self.cb_l = ttk.Checkbutton(self, text="左 L", variable=self.v_l,
                                    command=lambda: self._chan(0))
        self.cb_l.pack(side="left")
        self.cb_r = ttk.Checkbutton(self, text="右 R", variable=self.v_r,
                                    command=lambda: self._chan(1))
        self.cb_r.pack(side="left", padx=(2, 8))

        self.lb_time = ttk.Label(self, text="00:00.0 / 00:00.0",
                                 font=theme.FONT_MONO)
        self.lb_time.pack(side="left", padx=(0, 6))

        self.v_seek = tk.DoubleVar(value=0.0)
        self.sc = ttk.Scale(self, from_=0.0, to=1.0, orient="horizontal",
                            variable=self.v_seek, command=self._on_move)
        self.sc.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.sc.bind("<ButtonPress-1>", self._drag_on)
        self.sc.bind("<ButtonRelease-1>", self._drag_off)

        ttk.Label(self, text="音量", style="Faint.TLabel").pack(side="left")
        self.v_vol = tk.DoubleVar(value=80.0)
        self.sc_vol = ttk.Scale(self, from_=0, to=120, orient="horizontal",
                                variable=self.v_vol, length=70,
                                command=self._vol)
        self.sc_vol.pack(side="left", padx=(2, 0))

        # ---- 输出设备 ----
        # 内嵌播放器单独选声卡：试听"人声/伴奏"时可以送到 CABLE Input
        # （VB-Cable）走 FPGA→STM32→音响，而不是本机扬声器。
        try:
            import audio_io
            # ★ 剔除打不开的（Windows 的 WDM-KS 那一批），否则选了必然
            #   报 "permission denied / Unanticipated host error"。
            #   详见 host_app._enum_devices 的说明。
            self._outs, _drop = audio_io.usable_devices(
                audio_io.list_devices("output"), "output", channels=2)
        except Exception:
            self._outs = []
        self._dev_idx = [None] + [d[0] for d in self._outs]
        names = ["（系统默认）"] + ["%s [%s]" % (d[1], d[2]) for d in self._outs]
        ttk.Label(self, text="输出", style="Faint.TLabel").pack(side="left", padx=(8, 0))
        self.v_dev = tk.StringVar(value=names[0])
        self.cb_dev = ttk.Combobox(self, textvariable=self.v_dev, values=names,
                                   state="readonly", width=18,
                                   postcommand=self._refresh_devices)
        self.cb_dev.pack(side="left", padx=(2, 0))
        self.cb_dev.bind("<<ComboboxSelected>>", self._on_dev)

        self.lb_tag = ttk.Label(self, text="播放器（未载入）", style="Faint.TLabel")
        self.lb_tag.pack(side="left", padx=(8, 0))

        self._set_enabled(False)

    # ------------------------------------------------------------
    def _set_enabled(self, on):
        st = "normal" if on else "disabled"
        for w in (self.btn_play, self.btn_stop, self.cb_l, self.cb_r, self.sc):
            try:
                w.configure(state=st)
            except Exception:
                pass
        if not on:
            self.btn_play.configure(text="▶ 播放")
            self.lb_time.configure(text="00:00.0 / 00:00.0")
            self.lb_tag.configure(text="播放器（未载入）")

    def refresh(self):
        p = self.player
        on = p.has_data() and p.available
        self._set_enabled(on)
        if p.has_data():
            self.sc.configure(to=max(0.01, p.duration))
            txt = "播放器 · %s" % (p._tag or "音频")
            if not p.available:
                txt += "（缺 sounddevice）"
            self.lb_tag.configure(text=txt)
        self.tick()

    def _toggle(self):
        p = self.player
        if p.playing:
            p.pause()
            self.btn_play.configure(text="▶ 播放")
            self.log("播放器暂停")
        else:
            if p.play():
                self.btn_play.configure(text="⏸ 暂停")
            else:
                self.btn_play.configure(text="▶ 播放")

    def _stop(self):
        self.player.stop()
        self.btn_play.configure(text="▶ 播放")
        self.tick()
        self.log("播放器停止")

    def _chan(self, ch):
        self.player.set_channel(ch, self.v_l.get() if ch == 0 else self.v_r.get())
        self.log("播放器声道：左 %s / 右 %s"
                 % ("开" if self.v_l.get() else "关",
                    "开" if self.v_r.get() else "关"))

    def _vol(self, _v=None):
        self.player.set_volume(self.v_vol.get() / 100.0)

    def _on_dev(self, _e=None):
        """切输出设备：正在播就从新设备接着放（位置不变）。"""
        i = self.cb_dev.current()
        dev = self._dev_idx[i] if 0 <= i < len(self._dev_idx) else None
        self.player.set_device(dev)
        self.log("播放器输出设备 → %s" % self.v_dev.get())
        self.tick()

    def _refresh_devices(self):
        """每次展开下拉时重查一遍设备：插拔声卡后不用重启程序。

        PortAudio 的设备索引会随插拔变化，所以选中项要按**名字**重新映射，
        否则会出现"选的是 CABLE Input，实际打开的是别的口"。
        """
        try:
            import audio_io
            outs, _drop = audio_io.usable_devices(
                audio_io.list_devices("output"), "output", channels=2)
        except Exception:
            return
        cur = self.v_dev.get()
        self._outs = outs
        self._dev_idx = [None] + [d[0] for d in outs]
        names = ["（系统默认）"] + ["%s [%s]" % (d[1], d[2]) for d in outs]
        self.cb_dev.configure(values=names)
        if cur in names:
            self.v_dev.set(cur)
            i = names.index(cur)
            if self._dev_idx[i] != self.player.device:
                self.player.set_device(self._dev_idx[i])
        else:
            self.v_dev.set(names[0])
            self.player.set_device(None)

    def _on_move(self, _v=None):
        if self._drag:
            self.lb_time.configure(
                text="%s / %s" % (_fmt_time(self.v_seek.get()),
                                  _fmt_time(self.player.duration)))

    def _drag_on(self, _e=None):
        self._drag = True

    def _drag_off(self, _e=None):
        self._drag = False
        self.player.seek(self.v_seek.get())
        self.tick()

    def tick(self):
        p = self.player
        if not p.has_data():
            return
        if self.btn_play.cget("text") != ("⏸ 暂停" if p.playing else "▶ 播放"):
            self.btn_play.configure(text="⏸ 暂停" if p.playing else "▶ 播放")
        if not self._drag:
            self.v_seek.set(p.position)
            self.lb_time.configure(
                text="%s / %s" % (_fmt_time(p.position), _fmt_time(p.duration)))
