#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件变声的「输入格式 × 路径」矩阵测试，用真正的 App 代码跑。

    python tool/probe_file_formats.py

对每个用例记录三件事：载入是否成功、处理是否完成、弹了哪些对话框。
目的是把"某类文件必然报错"从口头猜测变成一张表 —— 用户报"还是报错"时，
先看他的文件是不是落在某个 ❌ 格子里。

覆盖：采样率（8k/44.1k/48k/96k）、位深（16/24/32f）、声道（1/2/6）、
容器（wav/flac/ogg/mp3）、空文件、非音频文件改名、中文+空格路径、
只读输出目录。
"""
import os
import shutil
import stat
import sys
import tempfile
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(os.path.dirname(HERE), "host")
sys.path.insert(0, HOST)
os.chdir(HOST)


def wav(path, sr=44100, ch=2, secs=1.0, width=2, kind="pcm"):
    n = max(1, int(sr * secs))
    t = np.arange(n) / float(sr)
    v = 0.4 * np.sin(2 * np.pi * 440 * t)
    m = np.stack([v] * ch, axis=1)
    if width == 2:
        pcm = np.clip(np.round(m * 32767), -32768, 32767).astype("<i2")
    elif width == 3:
        pcm = np.clip(np.round(m * 8388607), -8388608, 8388607).astype("<i4")
        pcm = pcm.view(np.uint8).reshape(n, ch * 4)[:, :3].copy()
    else:
        pcm = m.astype("<f4")
    with wave.open(path, "wb") as f:
        f.setnchannels(ch)
        f.setsampwidth(width)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())
    return path


def via_soundfile(path, sr=44100, ch=2, secs=1.0, fmt=None, subtype=None):
    import soundfile as sf
    n = int(sr * secs)
    t = np.arange(n) / float(sr)
    v = 0.4 * np.sin(2 * np.pi * 440 * t)
    m = np.stack([v] * ch, axis=1)
    sf.write(path, m, sr, format=fmt, subtype=subtype)
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="probe_fmt_")
    cases = []

    def add(name, path, outdir=None, expect="?"):
        cases.append((name, path, outdir or tmp, expect))

    add("wav 44.1k/16bit/2ch", wav(os.path.join(tmp, "a.wav")))
    add("wav 48k/16bit/2ch", wav(os.path.join(tmp, "b.wav"), 48000))
    add("wav 8k/16bit/1ch", wav(os.path.join(tmp, "c.wav"), 8000, 1))
    add("wav 96k/16bit/2ch", wav(os.path.join(tmp, "d.wav"), 96000))
    add("wav 48k/24bit/2ch", wav(os.path.join(tmp, "e.wav"), 48000, 2, 1.0, 3))
    add("wav 44.1k/32bit float", wav(os.path.join(tmp, "f.wav"), 44100, 2, 1.0, 4))
    add("wav 48k/16bit/6ch", wav(os.path.join(tmp, "g.wav"), 48000, 6))
    add("wav 空文件（0 帧）", wav(os.path.join(tmp, "h.wav"), 44100, 2, 0.0))

    try:
        add("flac 44.1k", via_soundfile(os.path.join(tmp, "i.flac"), fmt="FLAC"))
    except Exception as e:
        print("跳过 flac：%r" % (e,))
    try:
        add("ogg 44.1k", via_soundfile(os.path.join(tmp, "j.ogg"), fmt="OGG", subtype="VORBIS"))
    except Exception as e:
        print("跳过 ogg：%r" % (e,))
    try:
        add("mp3 44.1k", via_soundfile(os.path.join(tmp, "k.mp3"), fmt="MP3"))
    except Exception as e:
        print("跳过 mp3：%r" % (e,))

    # 非音频文件改名成 .wav
    bad = os.path.join(tmp, "l.wav")
    with open(bad, "wb") as f:
        f.write(b"this is not audio at all" * 100)
    add("文本改名 .wav", bad)

    # 中文 + 空格 + 括号路径
    d2 = os.path.join(tmp, "中文 目录（测试）")
    os.makedirs(d2, exist_ok=True)
    add("中文/空格/括号路径", wav(os.path.join(d2, "素材 01.wav")))

    # 只读输出目录
    ro = os.path.join(tmp, "readonly")
    os.makedirs(ro, exist_ok=True)

    import host_app
    from tkinter import messagebox as mb

    dialogs = []

    def rec(kind):
        def f(title, msg, **kw):
            dialogs.append("%s: %s | %s" % (kind, title, str(msg).replace("\n", " ")[:160]))
        return f

    mb.showerror = rec("error")
    mb.showwarning = rec("warn")
    mb.showinfo = rec("info")

    app = host_app.App()
    if app.v_module.get() != "pitch":
        app.v_module.set("pitch")

    rows = []
    for name, src, outdir, _ in cases:
        dialogs.clear()
        out = os.path.join(outdir, "out_%04d.wav" % (len(rows) + 1))
        app._load_file(src)
        loaded = app.file_data is not None
        done = False
        if loaded:
            app.v_outfile.set(out)
            app._do_process()
            t0 = time.time()
            while app._processing and time.time() - t0 < 120:
                app.update()
                time.sleep(0.01)
            app.update()
            done = os.path.exists(out)
        made = [p for p in os.listdir(outdir) if p.startswith("out_")] if os.path.isdir(outdir) else []
        rows.append((name, loaded, done, list(dialogs)))

    # 只读目录单独测：把目录设成不可写，再跑一次
    src = os.path.join(tmp, "a.wav")
    try:
        os.chmod(ro, stat.S_IREAD | stat.S_IEXEC)
        dialogs.clear()
        app._load_file(src)
        out_ro = os.path.join(ro, "ro_out.wav")
        app.v_outfile.set(out_ro)
        app._do_process()
        t0 = time.time()
        while app._processing and time.time() - t0 < 60:
            app.update()
            time.sleep(0.01)
        app.update()
        rows.append(("只读输出目录", True, os.path.exists(out_ro), list(dialogs)))
    except Exception as e:
        rows.append(("只读输出目录", True, False, ["测试本身失败 %r" % (e,)]))
    finally:
        try:
            os.chmod(ro, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except Exception:
            pass

    app.destroy()

    print("\n%-22s %-6s %-6s %s" % ("用例", "载入", "输出", "对话框"))
    print("-" * 78)
    for name, loaded, done, ds in rows:
        flag = "OK" if (loaded and done and not ds) else "!!"
        print("%s %-20s %-6s %-6s %s" %
              (flag, name, "是" if loaded else "否", "是" if done else "否",
               ("（无）" if not ds else ds[0])))
        for extra in ds[1:]:
            print("%24s %s" % ("", extra))
    print("\n临时目录：%s" % tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
