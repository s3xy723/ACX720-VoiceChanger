# -*- coding: utf-8 -*-
"""shot_ui.py —— 把上位机窗口渲染出来截个图，用于人工视觉自查。

用法： python tool/shot_ui.py out.png [组合下标]
跑完自动退出。数据是合成的，只为让频谱和灯板有东西可画。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import tkinter as tk

import dsp_core as dsp
import host_app


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "_shot.png"
    combo = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    app = host_app.App()
    app.update()
    app.geometry("1240x1170+40+20")

    combos = [(dsp.MODULE_PITCH, dsp.ENGINE_FPGA_DIRECT),
              (dsp.MODULE_PITCH, dsp.ENGINE_FPGA_MODEL),
              (dsp.MODULE_SPLIT, dsp.ENGINE_FPGA_DIRECT),
              (dsp.MODULE_SPLIT, dsp.ENGINE_LOCAL_MODEL)]
    mod, eng = combos[combo % len(combos)]
    app.v_module.set(mod)
    app.v_engine.set(eng)
    app.v_source.set(dsp.SRC_FILE)
    app._on_module_change()
    app._on_engine_change()
    app._refresh_labels()
    app.nb.select(1)                     # 文件页，控件更全

    # ---- 合成一条"人声"：基频 + 谐波 + 共振峰包络，左右略有差别 ----
    sr = 48000
    t = np.arange(int(sr * 1.5)) / sr
    f0 = 120.0 if mod == dsp.MODULE_SPLIT else 150.0
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 5.5 * t)
    x = np.zeros_like(t)
    for k in range(1, 26):
        a = 1.0 / k
        if k in (3, 4):
            a *= 2.2                    # 两个共振峰
        if k in (8, 9):
            a *= 1.8
        x += a * np.sin(2 * np.pi * f0 * k * t + 0.3 * k)
    x *= env * 0.12
    r = np.roll(x, int(0.0008 * sr)) * 0.9
    st = np.stack([x, r], axis=1).astype(np.float32)

    # ---- 灌进频谱：上=输入，下=输出 ----
    import spectrum as specmod
    app.spec.set_sr(sr)
    li = specmod.compute_spectrum(st[:, 0], sr)
    ri = specmod.compute_spectrum(st[:, 1], sr)
    lo = specmod.compute_spectrum(st[:, 0] * 0.8 + 0.02, sr)
    ro = specmod.compute_spectrum(st[:, 1] * 0.5, sr)
    app.spec.set_in(li, ri)
    app.spec.set_out(lo, ro)
    app.spec.draw()

    # ---- 四路波形：左=人声、右=伴奏（新分离语义的样子） ----
    app.scope.set_sr(sr)
    app.scope.set_in(st[:, 0], st[:, 1])
    voc = st[:, 0] * 0.7
    acc = st[:, 1] * 0.45 + 0.05 * np.sin(2 * np.pi * 90.0 * t)
    app.scope.set_out(voc, acc)
    app.scope.draw()

    # ---- 内嵌播放器：载入一段结果，展示进度/时间/左右声道开关 ----
    app.player.load(st, sr, "变声结果")
    app.player_bar.refresh()
    app.player.seek(0.6)
    app.player_bar.tick()

    # ---- 灯板：几种状态都点一个，好看也好验 ----
    st_map = [("dev_in", host_app.OK, "麦克风阵列"),
              ("dev_out", host_app.OK, "Realtek"),
              ("sr", host_app.OK, "48000 Hz"),
              ("engine", host_app.BUSY, "运行中"),
              ("sig_in", host_app.OK, "-18 dBFS"),
              ("sig_out", host_app.OK, "-16 dBFS"),
              ("clip_in_l", host_app.WARN, "削波 3 次"),
              ("clip_in_r", host_app.OK, "无削波"),
              ("clip_out_l", host_app.OK, "无削波"),
              ("clip_out_r", host_app.WARN, "削波 1 次"),
              ("buf", host_app.OK, "2 块"),
              ("dsp", host_app.OK, "就绪"),
              ("ai", host_app.IDLE, "未启用"),
              ("file", host_app.BUSY, "处理中 42%"),
              ("cache", host_app.OK, "12.4 MB"),
              ("sw", host_app.OK, "0x95")]
    for k, s, tip in st_map:
        app.leds.set(k, s, tip)
    app.leds.draw()

    app.lb_advice.configure(
        text="建议：输入左声道偶发削波（3 次）—— 把「输入增益」从 +12 dB 调低 2~3 dB；"
             "输出右声道削波 1 次，可忽略。")
    app._log("视觉自查：%s × %s" % (mod, eng))
    for i in range(6):
        app._log("[%02d] 这是一条日志样例，用于检查深色主题下的可读性与行距。" % i)

    app.update()
    app.update_idletasks()

    # 等布局稳定（Notebook 切换后要一两轮）
    # 截图前把窗口顶到最前：否则同屏的其它程序会盖在抓取区域上，
    # 截出来是别人的窗口（2026-10-20 实测被 Dism++ 盖住过一次）。
    app.attributes("-topmost", True)
    app.lift()
    try:
        app.focus_force()
    except Exception:
        pass
    for _ in range(4):
        app.update()
        time.sleep(0.08)
        app.update()
    time.sleep(0.35)

    x0 = app.winfo_rootx()
    y0 = app.winfo_rooty()
    w = app.winfo_width()
    h = app.winfo_height()

    from PIL import ImageGrab
    full = ImageGrab.grab(all_screens=True)
    # Windows 显示缩放（本机 125%）下，Tk 的 winfo_* 是逻辑像素，
    # ImageGrab 抓的是物理像素：不换算就会只截到窗口的 80%，
    # 右半边和底部被裁掉（看起来像界面溢出，其实是截图坐标错了）。
    sx = full.width / float(app.winfo_screenwidth())
    sy = full.height / float(app.winfo_screenheight())
    img = full.crop((int(x0 * sx), int(y0 * sy),
                     int((x0 + w) * sx), int((y0 + h) * sy)))
    img.save(out)
    print("saved %s  (%dx%d)  combo=%s x %s" % (out, w, h, mod, eng))
    app.attributes("-topmost", False)
    app.destroy()


if __name__ == "__main__":
    main()
