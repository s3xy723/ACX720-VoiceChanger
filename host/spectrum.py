# -*- coding: utf-8 -*-
"""
spectrum.py  ——  频谱显示

刻意不用 matplotlib：一是 EXE 体积能小一大截，二是 Canvas 直接画
在 20 fps 下比 Figure 重绘快得多，实时模式下 UI 不会拖后腿。

上下两条频带共用一条对数频率轴（40 Hz .. sr/2），72 根柱：
    上（蓝系）  输入  麦克风 / 文件原始信号
    下（橙系）  输出  经过效果链之后的信号

**每条频带里同时画左右两个声道**：每个频点的一格劈成两半，
左半边是 L（饱和色）、右半边是 R（同色系淡色）。
（深色底上不能用"深色=左、浅色=右"——深的那半会糊进背景，
所以改成"饱和 / 淡"配对，左右仍一眼可分。）

这样一张图同时回答两个问题：
  - 效果到底做了什么（上/下对比：变调搬位置、环调长边带、回声刻梳齿）
  - 左右到底一不一样（左半/右半对比：跑人声/伴奏分离时，上半两半一样高，
    下半左=人声、右=伴奏，各走各的）

对外接口
--------
    RingBuf              环形缓冲，音频回调里写、GUI 线程里读
    compute_spectrum()   一段时域样本 -> 72 根柱的 dB 数组
    DualSpectrum         一个 Canvas 同时画上下两条频谱、每条含左右声道
    QuadScope            四路时域波形：输入 L/R + 输出 L/R（2026-10-20 新增）
"""

import tkinter as tk

import numpy as np

import theme

NFFT = 1024          # FFT 点数：@48 kHz 约 21 ms，频率分辨率 47 Hz
NBAR = 72            # 柱子数量
F_MIN = 40.0         # 对数轴下限，人声以下的部分没意义
DB_MIN = -90.0
DB_MAX = 0.0

_WIN = np.hanning(NFFT).astype(np.float32)
_BINS_CACHE = {}


# ---------------------------------------------------------------- 环形缓冲

class RingBuf(object):
    """单写单读的定长环形缓冲。

    音频回调只做 push（O(n) 拷贝），GUI 线程只做 latest()（一次拼接），
    两边都不加锁 —— 最坏情况只是取到差一帧的数据，听感和显示都无影响。
    """

    def __init__(self, size=4096):
        self.size = int(size)
        self.buf = np.zeros(self.size, dtype=np.float32)
        self.p = 0
        self.filled = 0

    def reset(self):
        self.buf[:] = 0.0
        self.p = 0
        self.filled = 0

    def push(self, x):
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        n = x.size
        if n <= 0:
            return
        if n >= self.size:
            self.buf[:] = x[-self.size:]
            self.p = 0
            self.filled = self.size
            return
        end = self.p + n
        if end <= self.size:
            self.buf[self.p:end] = x
        else:
            cut = self.size - self.p
            self.buf[self.p:] = x[:cut]
            self.buf[:end - self.size] = x[cut:]
        self.p = end % self.size
        self.filled = min(self.size, self.filled + n)

    def latest(self):
        """按时间顺序返回最近 filled 个样本。"""
        if self.filled <= 0:
            return None
        if self.filled < self.size:
            return self.buf[:self.filled]
        return np.concatenate([self.buf[self.p:], self.buf[:self.p]])


# ---------------------------------------------------------------- FFT

def _bins_for(sr):
    """把 rfft 的 bin 下标按对数频率分组，返回 (NBAR, 2) 的 [起, 止) 表并缓存。

    低频段有个绕不过去的坑：72 个对数桶铺满 40 Hz .. 24 kHz，最下面那几
    桶宽度只有几 Hz，比 FFT 本身的分辨率（@48 kHz / 1024 点 = 46.9 Hz）
    还窄。这时同一根 bin 会落进好几个桶里，画出来就是一小段阶梯——
    这是真实分辨率所限，只能接受。

    千万别用"后一桶下标 = 前一桶 + 1"去强行拉开：那样整条轴会被
    一路推着往上跑，1 kHz 的峰会显示在 236 Hz 的位置上。
    """
    key = int(sr)
    b = _BINS_CACHE.get(key)
    if b is not None:
        return b
    freqs = np.fft.rfftfreq(NFFT, 1.0 / float(sr))
    fmax = float(sr) * 0.5
    edges = np.logspace(np.log10(F_MIN), np.log10(max(F_MIN * 1.1, fmax)), NBAR + 1)
    idx = np.clip(np.searchsorted(freqs, edges, side="left"), 0, freqs.size)
    pairs = np.empty((NBAR, 2), dtype=np.int64)
    for i in range(NBAR):
        a = int(idx[i])
        bnd = int(idx[i + 1])
        if bnd <= a:                       # 桶比一个 bin 还窄：共享这一个 bin
            a = min(a, freqs.size - 1)
            bnd = a + 1
        pairs[i, 0] = a
        pairs[i, 1] = min(bnd, freqs.size)
    _BINS_CACHE[key] = pairs
    return pairs


def compute_spectrum(x, sr):
    """时域样本 -> 长度 NBAR 的 dB 数组（每桶取峰值）。失败返回 None。"""
    if x is None:
        return None
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if x.size < 16:
        return None
    n = min(NFFT, x.size)
    seg = x[-n:]
    if n < NFFT:                      # 不足一帧时左侧补零，低频不受影响
        seg = np.concatenate([np.zeros(NFFT - n, dtype=np.float32), seg])
    sp = np.fft.rfft(seg * _WIN)
    mag = np.abs(sp) / (NFFT * 0.25)  # 归一化到满量程正弦 ≈ 0 dB
    db = 20.0 * np.log10(mag + 1e-9)
    pr = _bins_for(sr)
    out = np.empty(NBAR, dtype=np.float32)
    for i in range(NBAR):
        a = pr[i, 0]
        b = pr[i, 1]
        out[i] = float(np.max(db[a:b])) if b > a else DB_MIN
    return np.clip(out, DB_MIN, DB_MAX)


def spectrum_of_signal(x, sr, nseg=8):
    """整段信号（文件模式用）的平均频谱：切成 nseg 段分别算再取平均。

    用平均而不是单帧，是因为单帧只有 21 ms，正好落在音节间隙时
    会显示成一片空白，看起来像"没读到文件"。
    """
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if x.size < NFFT:
        return compute_spectrum(x, sr)
    step = max(1, x.size // nseg)
    acc = None
    cnt = 0
    for s in range(0, x.size - NFFT + 1, step):
        d = compute_spectrum(x[s:s + NFFT], sr)
        if d is None:
            continue
        acc = d if acc is None else acc + d
        cnt += 1
        if cnt >= nseg:
            break
    return None if acc is None else (acc / float(cnt))


# ---------------------------------------------------------------- 画布

# 深色底下的取色规则（详见 theme.py）：
#   L 声道 = 同色系的饱和色，R 声道 = 同色系的淡色。
# 早期版本用的是"深色=左、浅色=右"，那是白底上的规则；搬到深色底之后
# 深色那半会糊进背景里，所以改成"饱和 / 淡"的配对，语义不变。


class DualSpectrum(tk.Canvas):
    """上下两条频带（上=输入，下=输出），每条频带里同时画左右声道。

    每个频点的一格劈成左右两半：左半边画 L、右半边画 R。
    只有一路数据时（单声道源，或选了"只看左/右"）柱子画满整格。

    深色主题的画法：柱子用"本体 + 顶部 2 px 亮帽"，是频谱仪上最常见的
    样子 —— 比纯色矩形更容易读出瞬时峰值，而且只多一次 create_rectangle，
    20 fps 下没有可感知的开销（渐变分段实测会把绘制时间翻三倍，不值）。
    """

    SPLIT = 14          # 上下两区之间的间隔
    GAP = 1.0           # 左右半格之间的缝（像素）
    CAP_H = 2.0         # 亮帽高度

    COLORS = theme.SPEC
    PEAKS = theme.SPEC_PEAK
    CAP = {("in", 0): "#bfe4ff", ("in", 1): "#e8f6ff",
           ("out", 0): "#ffd2a0", ("out", 1): "#fff0dc"}   # 柱顶亮帽

    def __init__(self, master, height=260, **kw):
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightbackground", theme.BORDER)
        kw.setdefault("bg", theme.PANEL)
        tk.Canvas.__init__(self, master, height=height, **kw)
        # db[which][ch]：which = "in"/"out"，ch = 0 左 / 1 右；None 表示没数据
        self.db = {"in": [None, None], "out": [None, None]}
        self.peak = {"in": [np.full(NBAR, DB_MIN, dtype=np.float32) for _ in range(2)],
                     "out": [np.full(NBAR, DB_MIN, dtype=np.float32) for _ in range(2)]}
        self.sr = 48000
        # 注意：成员变量不能叫 _w —— 那是 tkinter 存放窗口路径的内部属性，
        # 子类里覆盖它之后所有几何操作都会拿 0 去当窗口名。
        self._cw = 0
        self._ch = height
        # 记下定时器 id：窗口销毁后 Tk 会取消不了匿名 after，回调再触发就
        # 抛 TclError("invalid command name")，退出时刷一屏 traceback。
        self._tick_id = None
        self.bind("<Configure>", self._on_cfg)
        self._tick_id = self.after(60, self._tick)

    # ----------------------------------------------------------------
    def _on_cfg(self, e):
        self._cw = e.width
        self._ch = e.height
        self.draw()

    def _tick(self):
        """峰值保持线的缓慢回落，让瞬态不至于一闪即逝。"""
        try:
            if not self.winfo_exists():
                self._tick_id = None
                return
        except Exception:
            self._tick_id = None
            return
        for which in ("in", "out"):
            for ch in (0, 1):
                np.maximum(self.peak[which][ch] - 0.9, DB_MIN,
                           out=self.peak[which][ch])
        self._tick_id = self.after(60, self._tick)

    def destroy(self):
        # 销毁时主动撤掉定时器，否则会有一条永久自重启的 after 留着
        if self._tick_id is not None:
            try:
                self.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        tk.Canvas.destroy(self)

    # ----------------------------------------------------------------
    def _set(self, which, l, r):
        if l is None and r is None:
            self.db[which] = [None, None]
            return
        self.db[which] = [l, r]
        if l is not None:
            self.peak[which][0] = np.maximum(self.peak[which][0], l)
        if r is not None:
            self.peak[which][1] = np.maximum(self.peak[which][1], r)

    def set_in(self, l, r=None):
        """输入频带。只给一个数组时画满整格（单声道 / 平均）。"""
        self._set("in", l, r)

    def set_out(self, l, r=None):
        """输出频带。"""
        self._set("out", l, r)

    def clear_band(self, which):
        self._set(which, None, None)
        for ch in (0, 1):
            self.peak[which][ch][:] = DB_MIN

    def clear(self):
        self.db = {"in": [None, None], "out": [None, None]}
        for which in ("in", "out"):
            for ch in (0, 1):
                self.peak[which][ch][:] = DB_MIN
        self.draw()

    # ----------------------------------------------------------------
    def _y_of(self, v, ytop, ybot):
        t = (float(v) - DB_MIN) / (DB_MAX - DB_MIN)
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        return ybot + (ytop - ybot) * t

    def _bar(self, v, xa, xb, ytop, ybot, color, cap):
        """一根柱子：本体 + 顶部 2 px 亮帽。

        亮帽是频谱仪的经典画法：瞬时峰值在哪一格，看帽子的位置就行，
        比纯色矩形好读，而且只多一次 create_rectangle。
        """
        if xb - xa < 0.6:
            return
        yv = self._y_of(v, ytop, ybot)
        if yv < ybot - 0.5:
            self.create_rectangle(xa, yv, xb, ybot, fill=color, outline="")
            if yv + self.CAP_H < ybot:
                self.create_rectangle(xa, yv, xb, yv + self.CAP_H,
                                      fill=cap, outline="")

    def draw(self):
        w = self._cw if self._cw > 10 else int(self.winfo_width())
        h = self._ch if self._ch > 10 else int(self.winfo_height())
        if w < 20 or h < 40:
            return
        self.delete("all")
        self.configure(bg=theme.PANEL)

        pad_l, pad_r, pad_t, pad_b = 40, 10, 18, 20
        gw = w - pad_l - pad_r
        band_h = (h - pad_t - pad_b - self.SPLIT) / 2.0
        if gw < 20 or band_h < 20:
            return

        y0_in, y1_in = pad_t, pad_t + band_h
        y0_out, y1_out = y1_in + self.SPLIT, y1_in + self.SPLIT + band_h

        # ---- 网格与频率刻度 ----
        fmax = float(self.sr) * 0.5
        log0 = np.log10(F_MIN)
        log1 = np.log10(max(F_MIN * 1.1, fmax))

        def x_of_f(f):
            return pad_l + gw * (np.log10(max(f, F_MIN)) - log0) / (log1 - log0)

        for dec in (100, 1000, 10000):
            if dec >= fmax:
                continue
            x = x_of_f(dec)
            self.create_line(x, y0_in, x, y1_out, fill=theme.SPEC_GRID)
            self.create_text(x, h - pad_b + 10,
                             text=("%d" % dec if dec < 1000 else "%dk" % (dec // 1000)),
                             fill=theme.SPEC_TEXT, font=theme.FONT_TINY,
                             anchor="n")

        for band in ((y0_in, y1_in), (y0_out, y1_out)):
            for dbv in (-20, -40, -60):
                y = band[1] + (band[0] - band[1]) * (dbv - DB_MIN) / (DB_MAX - DB_MIN)
                self.create_line(pad_l, y, w - pad_r, y, fill=theme.SPEC_GRID)
                self.create_text(pad_l - 5, y, text="%d" % dbv, anchor="e",
                                 fill=theme.SPEC_TEXT, font=theme.FONT_TINY)
            # 圆角边框比直角柔和，和卡片的视觉语言一致
            theme.round_rect(self, pad_l + 0.5, band[0] + 0.5,
                             w - pad_r - 0.5, band[1] - 0.5, 4,
                             outline=theme.SPEC_AXIS, fill="")

        # 左右声道图例：两个小色块 + L / R，放在每条频带的左上角内侧。
        # ★ 只画**有数据**的那几路：单声道源时如果还把 L+R 两个色块都画上，
        #   用户会以为右声道也在显示（实际上柱子是画满整格的）。
        for (ytop, ybot), which in (((y0_in, y1_in), "in"),
                                    ((y0_out, y1_out), "out")):
            l, r = self.db[which]
            lx = pad_l + 8
            ly = ytop + 9
            if l is not None:
                self.create_rectangle(lx, ly - 3, lx + 6, ly + 3,
                                      fill=self.COLORS[(which, 0)], outline="")
                self.create_text(lx + 10, ly, text="L", anchor="w",
                                 fill=theme.SPEC_TEXT, font=theme.FONT_TINY)
            if r is not None:
                ox = lx + (26 if l is not None else 0)
                self.create_rectangle(ox, ly - 3, ox + 6, ly + 3,
                                      fill=self.COLORS[(which, 1)], outline="")
                self.create_text(ox + 10, ly, text="R", anchor="w",
                                 fill=theme.SPEC_TEXT, font=theme.FONT_TINY)
            self.create_text(pad_l - 5, ytop + 9, text=which, anchor="e",
                             fill=(self.COLORS[(which, 0)]),
                             font=(theme.FONT_TINY[0], theme.FONT_TINY[1], "bold"))

        bw = gw / float(NBAR)
        half = bw / 2.0
        for which in ("in", "out"):
            l, r = self.db[which]
            pk_l = self.peak[which][0]
            pk_r = self.peak[which][1]
            ytop, ybot = (y0_in, y1_in) if which == "in" else (y0_out, y1_out)
            if l is None and r is None:
                self.create_text(pad_l + gw / 2, (ytop + ybot) / 2,
                                 text="（无信号）", fill=theme.SPEC_EMPTY,
                                 font=theme.FONT_SMALL)
                continue
            if l is not None and r is not None:
                # 左右并排：左半格 L、右半格 R
                for i in range(NBAR):
                    xa = pad_l + i * bw
                    xm = xa + half
                    xb = xa + bw
                    self._bar(l[i], xa + 0.5, xm - self.GAP / 2, ytop, ybot,
                              self.COLORS[(which, 0)], self.CAP[(which, 0)])
                    self._bar(r[i], xm + self.GAP / 2, xb - 0.5, ytop, ybot,
                              self.COLORS[(which, 1)], self.CAP[(which, 1)])
                    for pk, (pxa, pxb), ch in ((pk_l, (xa + 0.5, xm - self.GAP / 2), 0),
                                               (pk_r, (xm + self.GAP / 2, xb - 0.5), 1)):
                        p = float(pk[i])
                        if p > DB_MIN + 1.0:
                            yp = self._y_of(p, ytop, ybot)
                            self.create_line(pxa, yp, pxb, yp,
                                             fill=self.PEAKS[(which, ch)], width=1)
            else:
                db = l if l is not None else r
                ch = 0 if l is not None else 1
                for i in range(NBAR):
                    xa = pad_l + i * bw + 0.5
                    xb = xa + bw - 1.0
                    self._bar(db[i], xa, xb, ytop, ybot,
                              self.COLORS[(which, ch)], self.CAP[(which, ch)])
                    p = float(self.peak[which][ch][i])
                    if p > DB_MIN + 1.0:
                        yp = self._y_of(p, ytop, ybot)
                        self.create_line(xa, yp, xb, yp,
                                         fill=self.PEAKS[(which, ch)], width=1)

    def set_sr(self, sr):
        if sr and int(sr) != int(self.sr):
            self.sr = int(sr)
            _bins_for(self.sr)


# ================================================================ 四路波形
#
# 2026-10-20：原来是"上下两条频谱带，每格再劈成左/右两半"。
# 半格在 1240 px 的窗口里只有 3~4 px，跑分离模式时输出左右两路
# 一个是人声、一个是伴奏，能量差很大，半格根本不够看。现在改成
# **四条独立泳道**：输入 L / 输入 R / 输出 L / 输出 R，每条占满整宽。
#
# 为什么是时域波形而不是频谱：分离模式改成了"左出人声、右出伴奏"，
# 两条输出在时域上的包络差别（人声断续、伴奏连续）比频谱更直观；
# 而且四路同屏时，波形能一眼看出"哪一路没声、哪一路削顶、左右是否
# 真的分开"。想看频率内容仍可切回原来的频谱视图（界面上有下拉）。

class QuadScope(tk.Canvas):
    """四条独立泳道的时域波形：输入 L / 输入 R / 输出 L / 输出 R。

    自动量程是**四路共用**的：只按四路里最大的峰值定标，
    这样"输出 R 比输出 L 低 12 dB"在图上就是真的低 12 dB，
    不会因为每路各自拉满而看不出来。
    """

    LANES = (("in", 0, "输入 L"), ("in", 1, "输入 R"),
             ("out", 0, "输出 L"), ("out", 1, "输出 R"))
    COLORS = {
        ("in", 0): theme.SPEC[("in", 0)], ("in", 1): theme.SPEC[("in", 1)],
        ("out", 0): theme.SPEC[("out", 0)], ("out", 1): theme.SPEC[("out", 1)],
    }
    GAP = 5.0

    def __init__(self, master, height=250, **kw):
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightbackground", theme.BORDER)
        kw.setdefault("bg", theme.PANEL)
        tk.Canvas.__init__(self, master, height=height, **kw)
        self.data = {"in": [None, None], "out": [None, None]}
        self.sr = 48000
        self._cw = 0
        self._ch = height
        self._full = 0.05       # 当前满量程（四路共用），自动量程平滑用
        self.bind("<Configure>", self._on_cfg)

    # ----------------------------------------------------------------
    def _on_cfg(self, e):
        self._cw = e.width
        self._ch = e.height
        self.draw()

    def set_in(self, l, r=None):
        self.data["in"] = [None if l is None else np.asarray(l, dtype=np.float32).reshape(-1),
                           None if r is None else np.asarray(r, dtype=np.float32).reshape(-1)]

    def set_out(self, l, r=None):
        self.data["out"] = [None if l is None else np.asarray(l, dtype=np.float32).reshape(-1),
                            None if r is None else np.asarray(r, dtype=np.float32).reshape(-1)]

    def clear_band(self, which):
        self.data[which] = [None, None]

    def clear(self):
        self.data = {"in": [None, None], "out": [None, None]}
        self.draw()

    def set_sr(self, sr):
        if sr:
            self.sr = int(sr)

    # ----------------------------------------------------------------
    @staticmethod
    def _peak(x):
        if x is None or x.size == 0:
            return 0.0
        return float(np.max(np.abs(x)))

    def _auto_range(self):
        pk = 0.0
        for which in ("in", "out"):
            for x in self.data[which]:
                pk = max(pk, self._peak(x))
        target = max(pk * 1.08, 0.002)
        target = min(target, 1.05)
        # 变响立刻跟上（否则削顶看不见），变轻慢慢回落（波形不会一跳一跳）
        if target > self._full:
            self._full = target
        else:
            self._full = max(0.002, self._full * 0.985)
        return self._full

    def _lane_points(self, x, w):
        """返回 (xs, ys) 归一化到 [-1,1]，点数控制在 2w 以内。"""
        n = x.size
        if n <= 2 * w:
            idx = np.arange(n)
            xs = idx * (w / float(max(1, n - 1)))
            return xs, x
        # 长信号（文件整段 / 实时 4096 点）：按列取 min/max 包络
        bins = max(8, int(w))
        edges = np.linspace(0, n, bins + 1).astype(np.int64)
        centers = (edges[:-1] + edges[1:]) * 0.5
        xs = centers * (w / float(n))
        seg = np.abs(x)
        # 用 reduceat 取每列最大绝对值，再按原符号还原不了；直接取每列
        # 最大最小两个真实样本，画上下包络，波形形状仍然对。
        starts = edges[:-1]
        mx = np.maximum.reduceat(x, starts)
        mn = np.minimum.reduceat(x, starts)
        return xs, np.stack([mx, mn], axis=1)

    def draw(self):
        w = self._cw if self._cw > 10 else int(self.winfo_width())
        h = self._ch if self._ch > 10 else int(self.winfo_height())
        if w < 40 or h < 60:
            return
        self.delete("all")
        self.configure(bg=theme.PANEL)

        pad_l, pad_r, pad_t, pad_b = 58, 10, 8, 18
        gw = w - pad_l - pad_r
        lane_h = (h - pad_t - pad_b - self.GAP * 3) / 4.0
        if gw < 20 or lane_h < 10:
            return

        full = self._auto_range()
        lanes = []
        y = pad_t
        for i, (which, ch, label) in enumerate(self.LANES):
            lanes.append((which, ch, label, y, y + lane_h))
            y += lane_h + self.GAP

        for which, ch, label, ytop, ybot in lanes:
            yc = 0.5 * (ytop + ybot)
            # 泳道底 + 中线 + 1/2 量程线
            self.create_rectangle(pad_l, ytop, w - pad_r, ybot,
                                  fill=theme.PANEL_2, outline="")
            for yy in (ytop, yc, ybot):
                self.create_line(pad_l, yy, w - pad_r, yy,
                                 fill=(theme.SPEC_AXIS if yy != yc else theme.BORDER))
            col = self.COLORS[(which, ch)]
            x = self.data[which][ch]
            pk = self._peak(x)
            db = 20.0 * np.log10(pk) if pk > 1e-5 else -90.0
            self.create_text(pad_l - 6, yc - 6, text=label, anchor="e",
                             fill=col, font=theme.FONT_UI_B)
            self.create_text(pad_l - 6, yc + 7,
                             text=("%.0f dBFS" % db) if pk > 1e-5 else "无信号",
                             anchor="e", fill=theme.SPEC_TEXT, font=theme.FONT_TINY)
            if x is None or x.size == 0:
                self.create_text(pad_l + gw / 2, yc, text="（无信号）",
                                 fill=theme.SPEC_EMPTY, font=theme.FONT_SMALL)
                continue
            xs, ys = self._lane_points(x, gw)
            scale = (lane_h * 0.46) / max(full, 1e-6)
            if ys.ndim == 2:          # 包络：上沿 + 下沿两条线
                up = [(pad_l + float(a), yc - float(b) * scale)
                      for a, b in zip(xs, ys[:, 0])]
                dn = [(pad_l + float(a), yc - float(b) * scale)
                      for a, b in zip(xs, ys[:, 1])]
                if len(up) >= 2:
                    self.create_line(up, fill=col, width=1)
                if len(dn) >= 2:
                    self.create_line(dn, fill=col, width=1)
            else:
                pts = [(pad_l + float(a), yc - float(b) * scale)
                       for a, b in zip(xs, ys)]
                # ★ 2026-10-23：只有 1 个点（极短素材 / 面板还没布局完）时
                #   create_line 会直接抛 TclError: wrong # coordinates:
                #   expected at least 4, got 2 —— 而且是在 _load_file 的调用栈里
                #   抛的，表现成"一载入文件就报错、界面卡住"。宁可这一帧不画。
                if len(pts) >= 2:
                    self.create_line(pts, fill=col, width=1)

        # 量程文字 + 刻度
        self.create_text(w - pad_r, h - pad_b + 9,
                         text="四路共用 ±%.3f FS" % full, anchor="e",
                         fill=theme.SPEC_TEXT, font=theme.FONT_TINY)
        self.create_text(pad_l, h - pad_b + 9, text="时间 →（每路独立声道，未混音）",
                         anchor="w", fill=theme.SPEC_TEXT, font=theme.FONT_TINY)
