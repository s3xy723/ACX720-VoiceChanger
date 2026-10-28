# -*- coding: utf-8 -*-
"""
theme.py  ——  上位机的深色主题（集中管理配色 / 字体 / ttk 样式）

为什么单独一个文件：颜色以前散落在 host_app / spectrum / status_panel
三处，一共 45 个硬编码的 "#xxxxxx"。改主题要满文件找，而且浅色字配深色底
的对比度没人统一核对过。现在所有颜色只在这里定义一次，其它模块 import。

设计取向
--------
深色（"midnight slate"）：频谱和电平表长时间盯着看，深色底不刺眼，
而且亮色柱子在深色底上的对比度比白底更高（白底上 -90 dB 的暗柱会糊成灰）。

配色分四组：
   底色   BG / PANEL / PANEL_2 / BORDER      窗口 -> 卡片 -> 嵌套 -> 描边
   文字   FG / FG_DIM / FG_FAINT            主 -> 次 -> 弱（注释、提示）
   强调   ACCENT                             数值、主操作、选中态
   语义   OK / BUSY / WARN / ERR / IDLE      状态灯与告警，与 status_panel 共用

安全说明
--------
ttk.Style 的可用选项随 tkinter 版本变化（ clam 在 8.6.x 之间有增删，
例如 arrowcolor 只有 8.6.10+ 才有）。所以下面每一处 configure / map
都单独 try 过，不支持的项直接跳过 —— 宁可少美化一项，也不能因为换了个
Python 发行版就打不开窗口。
"""

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 调色板

BG        = "#16191f"   # 窗口底
PANEL     = "#1e232b"   # 卡片 / 面板底
PANEL_2   = "#262d37"   # 次级面板（嵌套块、输入框底）
BORDER    = "#333c48"   # 卡片描边 / 分隔线

FG        = "#e8ecf1"   # 主文本
FG_DIM    = "#9aa5b4"   # 次文本（说明、提示）
FG_FAINT  = "#6a7583"   # 弱文本（脚注、轴刻度）

ACCENT    = "#5b9dff"   # 强调：数值、主操作、选中态
ACCENT_DK = "#2b4a7d"   # 强调的暗色（描边、底色）
ON_ACCENT = "#0c1320"   # 压在强调色上的文字（深色，保证对比度）

OK        = "#3ecf8e"
BUSY      = "#5b9dff"
WARN      = "#f0b429"
ERR       = "#ff6b5e"
IDLE      = "#4d5765"

# 频谱两条频带。深色底上不能再用"深色=左、浅色=右"——那是在白底上的
# 规则；这里改成"同色系、L 用饱和色、R 用淡色"，语义不变（一眼能分出
# 左右），但在深色底上两半都亮、都看得清。
SPEC = {
    ("in", 0): "#3aa0ff", ("in", 1): "#a5dcff",      # 输入：蓝
    ("out", 0): "#ff9f43", ("out", 1): "#ffd9a3",    # 输出：琥珀
}
SPEC_PEAK = {
    ("in", 0): "#9fd0ff", ("in", 1): "#d8ecff",
    ("out", 0): "#ffc48a", ("out", 1): "#ffe8c9",
}
SPEC_GRID  = "#262d37"      # 网格线
SPEC_AXIS  = "#3a4450"      # 频带边框
SPEC_TEXT  = FG_FAINT       # 轴刻度
SPEC_EMPTY = FG_FAINT       # "（无信号）"

# ---------------------------------------------------------------- 字体

_FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "TkDefaultFont")


def _pick_family(root):
    """挑一个系统里真有的中文字体，挑不中就退回默认。"""
    try:
        have = set(root.tk.call("font", "families"))
    except Exception:
        return ""
    for f in _FONT_CANDIDATES:
        if f in have:
            return f
    return ""


FONT_UI = None      # apply() 里填
FONT_UI_B = None
FONT_SMALL = None
FONT_TINY = None
FONT_TITLE = None
FONT_MONO = None
FONT_MONO_B = None


# ---------------------------------------------------------------- 工具

def round_rect(c, x0, y0, x1, y1, r, **kw):
    """Canvas 圆角矩形：中间十字 + 四个四分之一圆。

    tkinter 的 Canvas 没有圆角矩形原语，只能这么拼。注意 fill 和 outline
    要一致地传给每一段，否则接缝处会露出底色。
    """
    r = max(0.0, min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if r <= 0.5:                      # 圆角退化就直接画方框，少 6 个图元
        c.create_rectangle(x0, y0, x1, y1, **kw)
        return
    c.create_rectangle(x0 + r, y0, x1 - r, y1, **kw)
    c.create_rectangle(x0, y0 + r, x1, y1 - r, **kw)
    c.create_arc(x0, y0, x0 + 2 * r, y0 + 2 * r, start=90, extent=90, style="pieslice", **kw)
    c.create_arc(x1 - 2 * r, y0, x1, y0 + 2 * r, start=0, extent=90, style="pieslice", **kw)
    c.create_arc(x0, y1 - 2 * r, x0 + 2 * r, y1, start=180, extent=90, style="pieslice", **kw)
    c.create_arc(x1 - 2 * r, y1 - 2 * r, x1, y1, start=270, extent=90, style="pieslice", **kw)


def _try(style, method, name, **kw):
    """单个样式配置失败就跳过，不让整个主题挂掉。"""
    try:
        getattr(style, method)(name, **kw)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 主题安装

def apply(root):
    """在 Tk 根窗口上装好深色主题。返回字体族名（可能为空串）。"""
    global FONT_UI, FONT_UI_B, FONT_SMALL, FONT_TINY, FONT_TITLE, FONT_MONO, FONT_MONO_B

    fam = _pick_family(root)
    FONT_UI    = (fam, 9)
    FONT_UI_B  = (fam, 9, "bold")
    FONT_SMALL = (fam, 8)
    FONT_TINY  = (fam, 8)
    FONT_TITLE = (fam, 14, "bold")
    FONT_MONO  = ("Consolas", 9)
    FONT_MONO_B = ("Consolas", 9, "bold")

    root.configure(bg=BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")       # clam 的样式可塑性最好；avail 里有就用
    except Exception:
        pass

    # ---- 容器 ----
    # 内容区一律用 PANEL：ttk 控件会把自己样式里的 background 画出来，
    # 所以卡片、面板、标签必须同色，只靠描边和标题区分层次。
    # 窗口根用 BG，四周露出一圈，像给内容加了层边框。
    _try(style, "configure", "TFrame", background=PANEL, borderwidth=0)
    _try(style, "configure", "Panel.TFrame", background=PANEL)
    _try(style, "configure", "Bar.TFrame", background=PANEL_2)

    # ---- 文本 ----
    _try(style, "configure", "TLabel", background=PANEL, foreground=FG,
         font=FONT_UI)
    _try(style, "configure", "Panel.TLabel", background=PANEL, foreground=FG)
    _try(style, "configure", "Dim.TLabel", background=PANEL, foreground=FG_DIM)
    _try(style, "configure", "Faint.TLabel", background=PANEL, foreground=FG_FAINT,
         font=FONT_SMALL)
    _try(style, "configure", "Accent.TLabel", background=PANEL, foreground=ACCENT,
         font=FONT_MONO_B)
    _try(style, "configure", "Title.TLabel", background=PANEL, foreground=FG,
         font=FONT_TITLE)
    _try(style, "configure", "Sub.TLabel", background=PANEL, foreground=FG_DIM,
         font=FONT_SMALL)
    _try(style, "configure", "Warn.TLabel", background=PANEL, foreground=WARN)
    _try(style, "configure", "Err.TLabel", background=PANEL, foreground=ERR)
    _try(style, "configure", "Ok.TLabel", background=PANEL, foreground=OK)
    _try(style, "configure", "Mono.TLabel", background=PANEL, foreground=FG,
         font=FONT_MONO)
    # 标题栏专用（深色条上的浅色字）
    _try(style, "configure", "HeaderTitle.TLabel", background=PANEL_2,
         foreground=FG, font=FONT_TITLE)
    _try(style, "configure", "HeaderSub.TLabel", background=PANEL_2,
         foreground=FG_DIM, font=FONT_SMALL)

    # ---- 按钮 ----
    _try(style, "configure", "TButton", background=PANEL_2, foreground=FG,
         font=FONT_UI, bordercolor=BORDER, lightcolor=PANEL_2, darkcolor=PANEL_2,
         focuscolor=ACCENT_DK, padding=(10, 4), relief="flat")
    _try(style, "map", "TButton",
         background=[("pressed", "#323b47"), ("active", "#2e3742"),
                     ("disabled", "#20252d")],
         foreground=[("disabled", "#5b6472")])
    _try(style, "configure", "Accent.TButton", background=ACCENT,
         foreground=ON_ACCENT, font=FONT_UI_B, bordercolor=ACCENT_DK,
         padding=(14, 5), relief="flat")
    _try(style, "map", "Accent.TButton",
         background=[("pressed", "#4a8bef"), ("active", "#6ba9ff"),
                     ("disabled", "#20252d")],
         foreground=[("disabled", "#5b6472")])
    _try(style, "configure", "Tool.TButton", background=PANEL, foreground=FG_DIM,
         font=FONT_SMALL, bordercolor=BORDER, padding=(8, 2), relief="flat")
    _try(style, "map", "Tool.TButton",
         background=[("pressed", "#323b47"), ("active", "#2a323c")])

    # ---- 单选 / 复选 ----
    for name in ("TRadiobutton", "TCheckbutton"):
        _try(style, "configure", name, background=PANEL, foreground=FG,
             font=FONT_UI,
             # 指示器底改浅色：clam 的对勾/圆点是深色，压在深灰底上几乎看不见，
             # 浅底上黑勾清晰（用户实测反馈可读性差）。
             indicatorbackground="#e3e9f0", indicatormargin=2,
             focuscolor=PANEL, padding=(2, 2))
        _try(style, "configure", "Panel." + name, background=PANEL,
             foreground=FG, font=FONT_UI, indicatorbackground="#e3e9f0")
    _try(style, "map", "TRadiobutton",
         indicatorcolor=[("selected", "#7fb3ff"), ("!selected", "#e3e9f0")],
         background=[("active", "#232a33")])
    _try(style, "map", "TCheckbutton",
         indicatorcolor=[("selected", "#7fb3ff"), ("!selected", "#e3e9f0")],
         background=[("active", "#232a33")])

    # ---- 下拉框 ----
    _try(style, "configure", "TCombobox", background=PANEL_2, foreground=FG,
         fieldbackground=PANEL_2, selectbackground=ACCENT_DK,
         selectforeground=FG, insertcolor=FG, arrowcolor=FG_DIM,
         bordercolor=BORDER, darkcolor=PANEL_2, lightcolor=PANEL_2,
         padding=(6, 3))
    _try(style, "map", "TCombobox",
         background=[("readonly", PANEL_2), ("disabled", "#20252d")],
         fieldbackground=[("readonly", PANEL_2), ("disabled", "#20252d")],
         foreground=[("disabled", "#5b6472")])

    # ---- 笔记本（标签页）----
    _try(style, "configure", "TNotebook", background=PANEL, bordercolor=BORDER,
         borderwidth=1, tabmargins=[0, 4, 0, 0], relief="flat")
    _try(style, "configure", "TNotebook.Tab", background=BG, foreground=FG_DIM,
         font=FONT_UI, padding=(16, 6), borderwidth=1, relief="flat")
    _try(style, "map", "TNotebook.Tab",
         background=[("selected", PANEL_2), ("active", "#232a33")],
         foreground=[("selected", FG)])

    # ---- 卡片（LabelFrame）----
    _try(style, "configure", "TLabelframe", background=PANEL, foreground=FG_DIM,
         font=FONT_SMALL, bordercolor=BORDER, borderwidth=1, relief="solid",
         labeloutside=False)
    _try(style, "configure", "TLabelframe.Label", background=PANEL,
         foreground=FG_DIM, font=FONT_SMALL)
    _try(style, "configure", "Card.TLabelframe", background=PANEL,
         foreground=FG_DIM, bordercolor=BORDER, borderwidth=1, relief="solid")
    _try(style, "configure", "Card.TLabelframe.Label", background=PANEL,
         foreground=ACCENT, font=FONT_UI_B)
    _try(style, "configure", "Flat.TLabelframe", background=PANEL,
         bordercolor=BORDER, borderwidth=1, relief="solid")

    # ---- 滑条 / 进度条 / 滚动条 ----
    _try(style, "configure", "Horizontal.TScale", background=PANEL,
         troughcolor=PANEL_2, sliderthickness=14, sliderlength=16,
         borderwidth=0)
    _try(style, "map", "Horizontal.TScale",
         background=[("pressed", ACCENT), ("active", "#7db3ff")],
         troughcolor=[("!disabled", PANEL_2)])
    _try(style, "configure", "Horizontal.TProgressbar", background=ACCENT,
         troughcolor=PANEL_2, borderwidth=0, thickness=8)
    _try(style, "configure", "Vertical.TScrollbar", background=PANEL_2,
         troughcolor=BG, bordercolor=BORDER, arrowcolor=FG_DIM,
         borderwidth=0, relief="flat")
    _try(style, "map", "Vertical.TScrollbar",
         background=[("pressed", "#3a4450"), ("active", "#333c48"),
                     ("disabled", "#20252d")])

    # ---- 菜单（有就配，没有也不影响）----
    _try(style, "configure", "TMenubutton", background=PANEL_2, foreground=FG,
         font=FONT_UI, bordercolor=BORDER, padding=(8, 3), relief="flat")

    return fam


# ---------------------------------------------------------------- 现成控件

def card(parent, text, **kw):
    """一张卡片：LabelFrame + 卡片样式，标题用强调色。

    卡片底色与内容区一致（PANEL），只靠描边和标题颜色分层 ——
    这样里面的 ttk.Label 不用逐个换样式也不会出现补丁。
    """
    kw.setdefault("style", "Card.TLabelframe")
    kw.setdefault("padding", (10, 8))
    return ttk.LabelFrame(parent, text=text, **kw)


def pill(parent, text, fg=ACCENT, bg=PANEL_2, font=None, height=22):
    """一个药丸形徽章（圆角矩形 + 文字），用于标题栏的组合/档位显示。

    返回 frame。调用方只需 grid/pack，内容已经画好。

    ★ 文字放在一个可变 dict 里，_draw 每次从 dict 读。
      早期版本把 text 捕获在闭包里，结果 set_pill_text 改完文字，
      紧接着一次 <Configure>（切模块会引起重排）就用**旧文字**重画了一遍，
      徽章显示的一直是创建时那个组合。这是踩过的坑。
    """
    fr = ttk.Frame(parent, style="TFrame")
    c = tk.Canvas(fr, height=height, highlightthickness=0, bg=bg)
    c.pack(fill="x", expand=True)
    f = font or FONT_UI_B
    st = {"text": text, "fg": fg}

    def _draw(_e=None):
        c.delete("all")
        w = c.winfo_width()
        if w < 8:
            return
        round_rect(c, 0.5, 1, w - 0.5, height - 1, (height - 2) / 2.0,
                   fill=bg, outline=BORDER)
        c.create_text(w / 2.0, height / 2.0, text=st["text"], fill=st["fg"], font=f)

    c.bind("<Configure>", _draw)
    c.update_idletasks()
    fr._pill_canvas = c
    fr._pill_state = st
    fr._pill_redraw = _draw
    return fr


def set_pill_text(fr, text, fg=None):
    """改 pill 的文字，并立刻重画（不等 Configure）。"""
    st = getattr(fr, "_pill_state", None)
    if st is None:
        return
    st["text"] = text
    if fg:
        st["fg"] = fg
    redraw = getattr(fr, "_pill_redraw", None)
    if redraw is not None:
        redraw()


def scrollable(parent, width=None, **kw):
    """一个带滚动条的容器：内容超高时右边出现细滚动条，不需要时自动收起。

    返回 (outer, inner)，控件往 inner 里加。

    为什么需要它：左侧参数面板的高度取决于当前选中的组合（分离 × 本地模型
    那一组参数最高，350 px）。窗口矮的时候不能把下面的卡片直接裁掉 ——
    「恢复默认参数」按钮被裁掉过一次，用户根本不知道有这功能。
    滚动条只在实际需要时 grid 出来，平时不占位置。
    """
    outer = ttk.Frame(parent, style="TFrame", width=width)
    if width:
        outer.grid_propagate(False)       # 宽度固定，否则会被最长的标签带着跑
    outer.rowconfigure(0, weight=1)
    outer.columnconfigure(0, weight=1)

    c = tk.Canvas(outer, highlightthickness=0, bg=PANEL, borderwidth=0)
    ys = ttk.Scrollbar(outer, orient="vertical", command=c.yview)
    c.configure(yscrollcommand=ys.set)
    c.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    ys.grid_remove()                      # 默认隐藏
    inner = ttk.Frame(c, style="TFrame")
    win = c.create_window((0, 0), window=inner, anchor="nw")

    def _sync(_e=None):
        c.configure(scrollregion=c.bbox("all"))
        if inner.winfo_reqheight() > c.winfo_height() + 1:
            ys.grid()
        else:
            ys.grid_remove()

    def _fit(e):
        # 让内容宽度跟着画布走，不然卡片会保持自己的"需要的宽度"而出现空白
        c.itemconfigure(win, width=e.width)
        _sync()

    def _wheel(e):
        c.yview_scroll(int(-1 * (e.delta / 120)), "units")

    inner.bind("<Configure>", _sync)
    c.bind("<Configure>", _fit)
    c.bind("<MouseWheel>", _wheel)         # 只劫持画布范围内的滚轮，不全局
    return outer, inner
