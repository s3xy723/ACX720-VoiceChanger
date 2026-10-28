# -*- coding: utf-8 -*-
"""verify_theme.py —— 深色主题的程序化视觉校验（不依赖截图，锁屏也能跑）

检查三件事：
  1. 频谱画布上的图元颜色与 theme.SPEC 对得上（柱体 / 亮帽 / 网格 / 边框 / 文字）
  2. 灯板上的圆点颜色与语义色对得上，且光晕存在
  3. 关键前景/背景搭配的 WCAG 对比度达标（深色主题最容易翻车的地方）
"""

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
HOST = os.path.join(ROOT, "host")
sys.path.insert(0, HOST)
sys.path.insert(0, HERE)

import dsp_core as dsp
import spectrum as specmod
import status_panel as spmod
import theme
import host_app


def _lum(hexc):
    h = hexc.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def main():
    app = host_app.App()
    app.update()
    ok = [0]
    bad = []

    def chk(cond, msg):
        if cond:
            ok[0] += 1
        else:
            bad.append(msg)

    # ---------- 1. 灌数据 ----------
    sr = 48000
    t = np.arange(int(sr * 1.2)) / sr
    x = sum((1.0 / k) * np.sin(2 * np.pi * 150.0 * k * t) for k in range(1, 20)) * 0.1
    st = np.stack([x, np.roll(x, 40) * 0.8], axis=1).astype(np.float32)
    app.spec.set_sr(sr)
    app.spec.set_in(specmod.compute_spectrum(st[:, 0], sr),
                    specmod.compute_spectrum(st[:, 1], sr))
    app.spec.set_out(specmod.compute_spectrum(st[:, 0] * 0.7, sr),
                     specmod.compute_spectrum(st[:, 1] * 0.4, sr))
    app.spec.draw()

    for k, s in (("dev_in", host_app.OK), ("clip_in_l", host_app.WARN),
                 ("engine", host_app.BUSY), ("ai", host_app.IDLE),
                 ("dsp", host_app.ERR)):
        app.leds.set(k, s, "x")
    app.leds.draw()
    app.update()

    # ---------- 2. 频谱图元 ----------
    # 注意：网格线是 line 图元、圆角边框看的是 outline 而不是 fill，
    # 早期版本只统计了 rectangle 的 fill，于是误报"网格缺失"。
    spec = app.spec
    fills, outlines, line_fills = {}, {}, {}
    for it in spec.find_all():
        t_ = spec.type(it)
        if t_ in ("rectangle", "oval"):
            c = spec.itemcget(it, "fill")
            if c:
                fills[c] = fills.get(c, 0) + 1
            o = spec.itemcget(it, "outline")
            if o:
                outlines[o] = outlines.get(o, 0) + 1
        elif t_ == "line":
            c = spec.itemcget(it, "fill")
            line_fills[c] = line_fills.get(c, 0) + 1
        elif t_ == "text":
            c = spec.itemcget(it, "fill")
            fills["text:" + c] = fills.get("text:" + c, 0) + 1

    # 柱体（4 种颜色都该出现：in-L/in-R/out-L/out-R）
    for key in (("in", 0), ("in", 1), ("out", 0), ("out", 1)):
        chk(fills.get(theme.SPEC[key], 0) > 10,
            "频谱缺少柱体色 %s = %s（实际 %d）" % (key, theme.SPEC[key],
                                                fills.get(theme.SPEC[key], 0)))
    # 亮帽
    cap_n = sum(fills.get(c, 0) for c in spec.CAP.values())
    chk(cap_n > 10, "频谱缺少亮帽（实际 %d）" % cap_n)
    # 网格 / 边框 / 文字
    chk(line_fills.get(theme.SPEC_GRID, 0) >= 6,
        "频谱网格线不足（实际 %d）" % line_fills.get(theme.SPEC_GRID, 0))
    chk(outlines.get(theme.SPEC_AXIS, 0) >= 2,
        "频谱频带边框缺失（实际 %d）" % outlines.get(theme.SPEC_AXIS, 0))
    chk(fills.get("text:" + theme.SPEC_TEXT, 0) >= 5, "频谱刻度文字缺失")
    # 深色底：柱子颜色必须明显亮于背景
    for key in (("in", 0), ("out", 0)):
        chk(contrast(theme.SPEC[key], theme.PANEL) > 2.5,
            "柱体色 %s 与面板底对比度不足（%.2f）" % (key, contrast(theme.SPEC[key],
                                                              theme.PANEL)))
    # L/R 必须能分辨（饱和度/亮度差）
    for band in ("in", "out"):
        d = abs(_lum(theme.SPEC[(band, 0)]) - _lum(theme.SPEC[(band, 1)]))
        chk(d > 0.03, "%s 频带左右两半亮度差太小（%.3f），暗底下分不出" % (band, d))

    # ---------- 3. 灯板 ----------
    leds = app.leds
    oval_fills = {}
    n_text = 0
    for it in leds.find_all():
        if leds.type(it) == "oval":
            c = leds.itemcget(it, "fill")
            oval_fills[c] = oval_fills.get(c, 0) + 1
        elif leds.type(it) == "text":
            n_text += 1
    for stt in (host_app.OK, host_app.WARN, host_app.BUSY, host_app.IDLE,
                host_app.ERR):
        chk(oval_fills.get(spmod.COLORS[stt], 0) >= 1,
            "灯板缺少 %s 色" % stt)
        chk(oval_fills.get(spmod.HALO[stt], 0) >= 1,
            "灯板缺少 %s 光晕" % stt)
    chk(n_text >= len(leds.items), "灯板文字数量不足（%d < %d）"
        % (n_text, len(leds.items)))
    for stt in (host_app.OK, host_app.WARN, host_app.ERR):
        chk(contrast(spmod.COLORS[stt], theme.PANEL_2) > 2.0,
            "灯 %s 与药丸底对比度不足（%.2f）" % (stt,
                                            contrast(spmod.COLORS[stt],
                                                     theme.PANEL_2)))

    # ---------- 4. 文本对比度（深色主题的核心指标）----------
    pairs = [("主文本 FG / PANEL", theme.FG, theme.PANEL, 7.0),
             ("次文本 FG_DIM / PANEL", theme.FG_DIM, theme.PANEL, 4.0),
             ("弱文本 FG_FAINT / PANEL", theme.FG_FAINT, theme.PANEL, 2.5),
             ("强调 ACCENT / PANEL", theme.ACCENT, theme.PANEL, 4.0),
             ("告警 WARN / PANEL", theme.WARN, theme.PANEL, 4.0),
             ("错误 ERR / PANEL", theme.ERR, theme.PANEL, 4.0),
             ("正常 OK / PANEL", theme.OK, theme.PANEL, 4.0),
             ("日志 FG_DIM / BG", theme.FG_DIM, theme.BG, 4.0),
             ("标题 FG / PANEL_2", theme.FG, theme.PANEL_2, 7.0),
             ("副标题 FG_DIM / PANEL_2", theme.FG_DIM, theme.PANEL_2, 4.0)]
    print("对比度（WCAG，正文 ≥4.5、大字 ≥3.0、脚注 ≥2.5 为合格）：")
    for name, a, b, need in pairs:
        r = contrast(a, b)
        flag = "PASS" if r >= need else "FAIL"
        print("  [%s] %-24s %.2f  （要求 ≥%.1f）" % (flag, name, r, need))
        chk(r >= need, "对比度不达标：%s = %.2f < %.1f" % (name, r, need))

    # ---------- 5. 窗口不裁剪 ----------
    app.nb.select(1)
    app.update()
    w = app.nb.nametowidget(app.nb.tabs()[1])
    last = w.winfo_children()[-1]
    chk(last.winfo_y() + last.winfo_height() <= w.winfo_height() + 1,
        "文件页最后一个控件被裁剪")
    app.nb.select(0)
    app.update()
    w0 = app.nb.nametowidget(app.nb.tabs()[0])
    last0 = w0.winfo_children()[-1]
    chk(last0.winfo_y() + last0.winfo_height() <= w0.winfo_height() + 1,
        "实时页最后一个控件被裁剪")
    chk(app.winfo_reqheight() <= app.winfo_height() + 2,
        "窗口高度装不下内容（需 %d，实际 %d）"
        % (app.winfo_reqheight(), app.winfo_height()))

    app.destroy()

    print("\n图元检查：%d 项通过" % ok[0])
    if bad:
        print("未通过 %d 项：" % len(bad))
        for b in bad:
            print("  - " + b)
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
