# -*- coding: utf-8 -*-
"""
status_panel.py  ——  状态校验指示灯板

一排彩色圆点，每一盏对应一项可判定的检查。颜色语义固定：
    灰 idle   未启用 / 还没轮到它
    绿 ok     检查通过
    蓝 busy   进行中
    黄 warn   能用但有问题（电平偏低、有削波、板上档位有偏差…）
    红 err    阻断项，必须处理

之所以单独做成 Canvas 而不是一堆 ttk.Label：12 盏灯要每 50 ms 全量刷新，
用 Canvas 一次 delete + 重画，比操作 12×2 个 widget 省太多开销。

深色主题下的画法：每盏灯是「药丸形底板 + 带光晕的圆点」。
光晕用一圈放大的暗色圆模拟 —— tkinter 的 Canvas 没有半透明填充，
只能这么近似；效果是灯在暗处"发亮"，比一个实心点更像仪器。
"""

import time

import tkinter as tk

import theme

IDLE = "idle"
OK = "ok"
BUSY = "busy"
WARN = "warn"
ERR = "err"

COLORS = {
    IDLE: theme.IDLE,
    OK: theme.OK,
    BUSY: theme.BUSY,
    WARN: theme.WARN,
    ERR: theme.ERR,
}
# 光晕：同色但压暗，画在圆点下面当影子
HALO = {
    IDLE: "#2b323b",
    OK: "#173d2e",
    BUSY: "#1d3355",
    WARN: "#4a3510",
    ERR: "#4d221d",
}

CELL_W = 152
CELL_H = 26
RADIUS = 4.6


class LedBoard(tk.Canvas):
    """items: [(key, 显示名, 默认提示), ...]，cols 列自动换行。

    高度不能只按"请求的列数"算：窗口窄的时候 draw() 会自动减列（保证每格
    仍有可读宽度），行数随之变多；如果高度还是按请求列数定的，多出来的行
    会**画在画布外面被裁掉**——不报错，只是灯不见了。
    所以每次布局变化都按实际列数把画布高度补回去。
    """

    def __init__(self, master, items, cols=6, **kw):
        self.items = list(items)
        self.cols = int(cols)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bg", theme.PANEL)
        tk.Canvas.__init__(self, master,
                           height=self._rows_for(self.cols) * CELL_H + 8, **kw)
        self.state = {k: (IDLE, "") for k, _, _ in self.items}
        # 不能叫 _w：那是 tkinter 存放窗口路径的内部属性
        self._cw = 0
        # ★ 性能关键：50 ms 轮询里 set() 绝大多数时候写的都是同样的值，
        #   不加 diff 的话 draw() 每次都要全量 delete+重建 70+ 个 canvas
        #   元素（每秒 20 遍），这就是灯板把界面拖卡顿的原因。
        #   状态签名没变就跳过重画；窗口尺寸变化走 Configure 强制重画。
        self._draw_sig = None
        self._last_draw = 0.0
        self.bind("<Configure>", lambda e: (setattr(self, "_cw", e.width), self.draw()))

    # ----------------------------------------------------------------
    def _rows_for(self, cols):
        cols = max(1, int(cols))
        return (len(self.items) + cols - 1) // cols

    def _sized_cols(self, w):
        """宽度不够就减列，保证每格仍有可读宽度。"""
        return max(1, min(self.cols, int(w) // CELL_W))

    # ----------------------------------------------------------------
    def set(self, key, state, tip=""):
        val = (state, tip)
        if self.state.get(key) != val:
            self.state[key] = val

    def reset_all(self, state=IDLE):
        for k in self.state:
            self.state[k] = (state, "")
        self._draw_sig = None          # 强制下次重画

    # ----------------------------------------------------------------
    def draw(self):
        w = self._cw if self._cw > 10 else int(self.winfo_width())
        # 状态签名（所有灯的 state/tip + 宽度）没变就不重画
        sig = (w, tuple(self.state[k] for k, _, _ in self.items))
        now = time.time()
        if sig == self._draw_sig:
            return
        # 节流：实时模式下计数控件的 tip 每 50 ms 都在变，光 diff 压不住
        # 重画频率；但 poll 50 ms 一轮，直接跳过即可，下一轮自然会补画，
        # 不会停留在旧画面。120 ms ≈ 最多 ~7 fps 的灯板刷新，肉眼无感。
        if now - self._last_draw < 0.12:
            return
        self._draw_sig = sig
        self._last_draw = now
        self.delete("all")
        w = self._cw if self._cw > 10 else int(self.winfo_width())
        if w < 40:
            return
        cols = self._sized_cols(w)
        need_h = self._rows_for(cols) * CELL_H + 8
        if abs(int(self.winfo_reqheight()) - need_h) > 1:
            self.configure(height=need_h)      # 行数变多就把高度补上，别把灯裁掉
        cw = w / float(cols)
        for i, (key, name, _dflt) in enumerate(self.items):
            st, tip = self.state.get(key, (IDLE, ""))
            r, c = divmod(i, cols)
            x = c * cw
            y = r * CELL_H + 4
            # 药丸底板：比背景亮一档，灯和文字都落在它上面
            theme.round_rect(self, x + 1.5, y + 1.5, x + cw - 3.5, y + CELL_H - 2.5,
                             (CELL_H - 4) / 2.0, fill=theme.PANEL_2,
                             outline=theme.BORDER)
            cx = x + 16
            cy = y + CELL_H / 2.0
            col = COLORS.get(st, COLORS[IDLE])
            self.create_oval(cx - RADIUS - 2.6, cy - RADIUS - 2.6,
                             cx + RADIUS + 2.6, cy + RADIUS + 2.6,
                             fill=HALO.get(st, HALO[IDLE]), outline="")
            self.create_oval(cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS,
                             fill=col, outline="")
            label = name if not tip else "%s：%s" % (name, tip)
            self.create_text(cx + RADIUS + 7, cy, text=label, anchor="w",
                             fill=theme.FG if tip else theme.FG_DIM,
                             font=theme.FONT_SMALL)


# ---------------------------------------------------------------- 图例

def legend(master):
    """颜色说明条，放在灯板下面。"""
    f = tk.Frame(master, bg=theme.PANEL)
    for st, name in ((OK, "正常"), (BUSY, "进行中"), (WARN, "注意"),
                     (ERR, "异常"), (IDLE, "未启用")):
        c = tk.Canvas(f, width=13, height=13, highlightthickness=0, bg=theme.PANEL)
        c.create_oval(2, 2, 11, 11, fill=COLORS[st], outline="")
        c.pack(side="left")
        tk.Label(f, text=name, font=theme.FONT_SMALL, fg=theme.FG_DIM,
                 bg=theme.PANEL).pack(side="left", padx=(2, 10))
    return f
