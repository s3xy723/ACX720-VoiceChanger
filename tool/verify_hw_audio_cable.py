#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用真实路径的链路测试：播放器 → CABLE Input → 桥接程序 → COM4 → FPGA → MCU → 喇叭。

与 `_hw_play_and_record.py` 用同一套"门控 ON/OFF"判据，区别只是声音不是我们直接
串口发帧，而是**送到 VB-Cable 的播放端**，由 `fpga_audio_bridge.py` 转发 —— 这才是
上位机里点"播放"时真正的走向。

同时把桥接程序的 stdout/stderr 抓回来，作为"日志回传"这一段。
"""
import os
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
MIC_SR = 48000
ON_SEC = 0.7
OFF_SEC = 0.7
CYCLES = 4
PRE_ROLL = 1.0


def _pick(pred, kind="max_output_channels"):
    for i, d in enumerate(sd.query_devices()):
        if d.get(kind, 0) > 0 and pred(d.get("name", "")):
            return i, d.get("name")
    return None, None


def _level_at(buf, sr, freq):
    buf = np.asarray(buf, dtype=np.float64)
    if buf.size < 256:
        return -120.0
    x = buf - buf.mean()
    w = np.hanning(buf.size)
    k = np.arange(buf.size)
    z = np.sum(x * w * np.exp(-1j * 2 * np.pi * freq / sr * k))
    return 20.0 * np.log10(abs(z) * 2.0 / max(1e-9, np.sum(w)) + 1e-12)


def _autoport():
    """CH340 的串口号会随插拔漂移（这次 COM5、上次 COM4），写死就变成
    '帧全发了、板子没反应'。机器上只有一个 CH340 时直接用它。"""
    try:
        from serial.tools import list_ports
        hits = [p.device for p in list_ports.comports()
                if "1A86" in (p.hwid or "").upper()
                or "CH340" in (p.description or "").upper()]
        if len(hits) == 1:
            return hits[0]
    except Exception:
        pass
    return "COM4"


def main():
    # 形如：verify_hw_audio_cable.py 1000 [--port COM5]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    freq = float(args[0]) if args else 1000.0
    amp = 0.9
    port = _autoport()
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]

    mic, mic_name = _pick(lambda n: "麦克风" in n, "max_input_channels")
    cab, cab_name = _pick(lambda n: "CABLE Input" in n, "max_output_channels")
    print("麦克风：idx=%s %s" % (mic, mic_name))
    print("CABLE Input：idx=%s %s" % (cab, cab_name))
    if mic is None or cab is None:
        print("RESULT: INCONCLUSIVE 缺麦克风或 CABLE Input")
        return 2

    bridge = subprocess.Popen(
        [sys.executable, "-u", os.path.join(HERE, "fpga_audio_bridge.py"), "--port", port],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print("桥接程序 pid=%d，等 2.5 s 初始化…" % bridge.pid)
    time.sleep(2.5)

    rec = {"buf": []}
    stop = threading.Event()

    def _cap():
        with sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32",
                            device=mic, blocksize=2048) as st:
            while not stop.is_set():
                b, _ = st.read(2048)
                rec["buf"].append(b[:, 0])

    th = threading.Thread(target=_cap, daemon=True)
    th.start()
    time.sleep(PRE_ROLL)

    marks = []
    sr_out = 48000
    t0 = time.time()
    try:
        with sd.OutputStream(samplerate=sr_out, channels=2, dtype="float32",
                             device=cab, blocksize=2048) as out:
            for _c in range(CYCLES):
                for on in (True, False):
                    s = time.time() - t0
                    n = int((ON_SEC if on else OFF_SEC) * sr_out)
                    t = np.arange(n) / float(sr_out)
                    v = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32) if on \
                        else np.zeros(n, dtype=np.float32)
                    out.write(np.stack([v, v], axis=1))
                    marks.append((s, time.time() - t0, on))
    except Exception as e:
        print("播放失败：%r" % (e,))

    time.sleep(0.4)
    stop.set(); th.join(timeout=3.0)

    blog = ""
    try:
        bridge.terminate()
        blog = bridge.stdout.read().decode("utf-8", "replace")
    except Exception:
        pass
    print("\n--- 桥接程序日志（最后 10 行）---")
    for ln in blog.splitlines()[-10:]:
        print("   " + ln[:110])

    y = np.concatenate(rec["buf"]) if rec["buf"] else np.zeros(0)

    def win_lvl(f, on):
        v = []
        for s, e, flag in marks:
            if flag is not on:
                continue
            # ★ 修正（2026-10-22）：原来是 `+ PRE_ROLL + 2.5`，多加了 2.5 s。
            #   桥接程序的 2.5 s 初始化发生在**录音线程启动之前**（上面第 58~62 行，
            #   先 Popen 再 sleep 2.5 s，之后才 th.start()），所以它根本不在录音
            #   时间轴里；而 PRE_ROLL 已经被 t0 的取法吸收了一次（t0 是在
            #   sleep(PRE_ROLL) 之后才取的，marks 本身就相对 t0）。两个都加上
            #   等于把分析窗整体平移 2.5 s —— ON+OFF 周期只有 1.4 s，
            #   2.5 s mod 1.4 s = 1.1 s，正好落进另一半 → ON/OFF 被判反。
            #   现象就是"明明出声了，脚本却报 -19 dB"。
            mid = (s + e) / 2.0 + PRE_ROLL
            i0, i1 = int((mid - 0.25) * MIC_SR), int((mid + 0.25) * MIC_SR)
            if 0 <= i0 < i1 <= y.size:
                v.append(_level_at(y[i0:i1], MIC_SR, f))
        return float(np.mean(v)) if v else -120.0

    print("\n%-8s %10s %10s %10s" % ("频点", "ON", "OFF", "ON−OFF"))
    res = {}
    for f in (freq, freq * 1.5, 250.0):
        res[f] = win_lvl(f, True) - win_lvl(f, False)
        print("%-8.0f %9.1fd %9.1fd %+9.1f dB" % (f, win_lvl(f, True), win_lvl(f, False), res[f]))
    d = res[freq]
    ctrl = max(res[f] for f in res if abs(f - freq) > 1.0)
    print("\n主频点 %+.1f dB，对照 %+.1f dB" % (d, ctrl))
    if d > 12.0 and d - ctrl > 8.0:
        print("RESULT: PASS 走 CABLE Input → 桥接 → COM4 这条路也能出声")
        return 0
    print("RESULT: FAIL 这条路听不到声（ON−OFF %+.1f dB）" % d)
    return 1


if __name__ == "__main__":
    sys.exit(main())
