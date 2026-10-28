#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在**不点界面**的情况下跑一遍「文件变声」的真实代码路径，把报错抓出来。

    python tool/probe_file_mode.py                # 默认：44.1 kHz 立体声 wav，变调模块
    python tool/probe_file_mode.py --sr 48000 --module 分离 --engine 中置
    python tool/probe_file_mode.py --keep         # 保留临时目录（看输出文件）

它做的是：
    1. 造一个 3 秒的测试 wav（可以指定采样率/声道）
    2. 实例化真正的 App（tk.Tk 子类），把 messagebox 换成记录器
    3. 调 App._load_file() + App._do_process()，然后 pump 事件循环到结束
    4. 打印 App 日志框里的全部内容 + 弹过的对话框

这样"界面上弹了个报错"就变成"终端里一段可复制的文本"，不用再靠截图猜。
"""
import argparse
import os
import sys
import tempfile
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(os.path.dirname(HERE), "host")
sys.path.insert(0, HOST)
os.chdir(HOST)


def make_wav(path, sr=44100, ch=2, secs=3.0, freq=440.0):
    n = int(sr * secs)
    t = np.arange(n) / float(sr)
    # 两路不同频率，能顺便看出左右是否搞反
    v = np.stack([0.5 * np.sin(2 * np.pi * freq * t),
                  0.4 * np.sin(2 * np.pi * freq * 1.5 * t)], axis=1)[:, :ch]
    pcm = np.clip(np.round(v * 32767), -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as f:
        f.setnchannels(ch)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sr", type=int, default=44100)
    ap.add_argument("--ch", type=int, default=2)
    ap.add_argument("--src", default=None, help="直接测这个音频文件（不造测试素材）")
    ap.add_argument("--module", default=None, help="变调 / 分离 / 环形调制 / 回声")
    ap.add_argument("--engine", default=None, help="FPGA 内部模型 / 计算机本地模型 / 中置…")
    ap.add_argument("--outdir", default=None, help="输出目录（默认同输入目录）")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--no-gui-loop", action="store_true")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="probe_file_")
    if a.src:
        src = a.src
    else:
        src = make_wav(os.path.join(tmp, "probe_in.wav"), a.sr, a.ch)
    outdir = a.outdir if a.outdir else tmp
    out = os.path.join(outdir, "probe_out.wav")
    print("输入 %s" % src)
    print("输出 %s" % out)

    import host_app
    from tkinter import messagebox as _mb

    dialogs = []
    for name in ("showerror", "showwarning", "showinfo"):
        setattr(_mb, name, (lambda kind: (lambda title, msg, **kw:
                                         dialogs.append((kind, title, msg))))(name))

    app = host_app.App()
    try:
        if a.module:
            app.v_module.set(a.module)
        if a.engine:
            app.v_engine.set(a.engine)
        print("模块=%s 引擎=%s" % (app.v_module.get(), app.v_engine.get()))

        app._load_file(src)
        if app.file_data is None:
            print("!! 载入失败，不再继续")
        else:
            app.v_outfile.set(out)
            app._do_process()
            t0 = time.time()
            while app._processing and time.time() - t0 < 600:
                app.update()
                time.sleep(0.02)
            if app._processing:
                print("!! 处理超过 600 s 仍未结束")
            app.update()
    finally:
        logtxt = ""
        try:
            logtxt = app.log.get("1.0", "end")
        except Exception:
            pass
        try:
            app.destroy()
        except Exception:
            pass

    print("\n===== 界面日志 =====")
    for line in logtxt.splitlines():
        if line.strip():
            print("  " + line)
    print("\n===== 弹过的对话框 =====")
    if not dialogs:
        print("  （无）")
    for kind, title, msg in dialogs:
        print("  [%s] %s\n      %s" % (kind, title, str(msg).replace("\n", "\n      ")))

    ok = os.path.exists(out)
    print("\n输出文件：%s (%s)" % (out, "已生成" if ok else "**没有生成**"))
    if ok:
        try:
            with wave.open(out, "rb") as f:
                print("  声道=%d 采样率=%d 帧数=%d 位深=%d bit" %
                      (f.getnchannels(), f.getframerate(), f.getnframes(),
                       f.getsampwidth() * 8))
        except Exception as e:
            print("  读回失败：%r" % (e,))
    if not a.keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("临时目录保留：%s" % tmp)
    return 0 if (ok and not dialogs) else 1


if __name__ == "__main__":
    sys.exit(main())
