#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量「直通档」下喇叭的实际输出质量，把"杂音"拆成三类可分辨的东西。

    三类杂音的指纹
    --------------
    A. 谐波失真（2/3/4 次谐波）：模拟段非线性（功放/喇叭/DAC 后级）
       —— 能量集中在 f0 的整数倍上，且随音量上升。
    B. 12~22 kHz 的宽带能量：DAC 零阶保持的镜像 + 24.414 kHz 采样带来的
       混叠残留，被后级非线性"折"下来就成了沙沙声。
    C. 冲击（click/pop）：要么数字域丢字/重字（FIFO 欠载），要么模拟接触不良
       —— 表现为 12~22 kHz 短时能量的孤立尖峰，且**只在有信号时**听得出
       （静音时同样的丢字只是"零丢零"，听不见）。

    所以本脚本同时录两段：静音段（看底噪）和 1 kHz 持续音段（看 A/B/C），
    再可选地让 PC 扬声器放同一个音做基线（--ref），用同一个麦克风量，
    这样麦克风本身的味底可以扣掉。

用法：
    python tool/measure_pass_quality.py                 # CABLE → 桥接 → FPGA → 喇叭
    python tool/measure_pass_quality.py --ref           # 顺带量 PC 扬声器做对照
    python tool/measure_pass_quality.py --freq 440
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
TONE_SEC = 6.0
LEAD_SEC = 1.0
TAIL_SEC = 1.0


def _pick(pred, kind):
    for i, d in enumerate(sd.query_devices()):
        if d.get(kind, 0) > 0 and pred(d.get("name", "")):
            return i, d.get("name")
    return None, None


def _autoport():
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


def record(dev, seconds):
    """录一段，返回 np.float32 单声道。"""
    buf = []
    stop = threading.Event()

    def cap():
        with sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32",
                            device=dev, blocksize=1024) as st:
            while not stop.is_set():
                b, _ = st.read(1024)
                buf.append(b[:, 0].copy())

    th = threading.Thread(target=cap, daemon=True)
    th.start()
    time.sleep(seconds)
    stop.set()
    th.join(timeout=3.0)
    return np.concatenate(buf) if buf else np.zeros(0)


def play_tone(dev, freq, amp, seconds):
    sr = 48000
    n = int(seconds * sr)
    t = np.arange(n) / float(sr)
    v = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    with sd.OutputStream(samplerate=sr, channels=2, dtype="float32",
                         device=dev, blocksize=2048) as out:
        # 先灌 0.2 s 静音，保证 Windows 混音器已经把流跑起来
        z = np.zeros((int(0.2 * sr), 2), dtype=np.float32)
        out.write(z)
        out.write(np.stack([v, v], axis=1))


def analyze(y, freq, tag):
    """对一段录音做三项指标。返回 dict。"""
    x = np.asarray(y, dtype=np.float64)
    if x.size < MIC_SR // 2:
        print("%s: 录音太短" % tag)
        return None
    x = x - x.mean()
    n = 1 << int(np.floor(np.log2(min(x.size, 1 << 17))))
    seg = x[:n] * np.hanning(n)
    V = np.abs(np.fft.rfft(seg))
    fr = np.fft.rfftfreq(n, 1.0 / MIC_SR)
    df = fr[1] - fr[0]

    def band_pow(f0, w=25.0):
        m = np.abs(fr - f0) < max(w, 2 * df)
        return float((V[m] ** 2).sum())

    base = band_pow(freq)
    l_base = 10 * np.log10(max(base, 1e-20) + 1e-20)
    harm = {}
    for k in range(2, 9):
        h = freq * k
        if h > 20000:
            break
        harm[k] = 10 * np.log10(max(band_pow(h), 1e-20) + 1e-20) - l_base
    hf = float((V[(fr >= 12000) & (fr <= 22000)] ** 2).sum())
    l_hf = 10 * np.log10(max(hf, 1e-20) + 1e-20) - l_base

    # 冲击检测：12~22 kHz 的短时能量，孤立尖峰 = click
    h = _bandlimit(x, 12000.0, 22000.0)
    seg_n = int(0.002 * MIC_SR)
    if h.size >= seg_n * 4:
        k = h.size // seg_n
        e = np.array([np.sqrt(np.mean(h[i * seg_n:(i + 1) * seg_n] ** 2))
                      for i in range(k)])
        med = np.median(e) + 1e-12
        clicks = int(np.sum(e > 20.0 * med))
        peak_ratio = float(e.max() / med)
    else:
        clicks, peak_ratio = 0, 0.0

    rms = 10 * np.log10(max(float(np.mean(x ** 2)), 1e-20) + 1e-20)
    return dict(base=l_base, rms=rms, harm=harm, hf=l_hf,
                clicks=clicks, peak_ratio=peak_ratio, df=df)


def _bandlimit(x, lo, hi):
    """FFT 域带限（没有 scipy，就不用 FIR 了）：
    把 12~22 kHz 之外的分量置零再逆变换，得到"只剩高频宽带成分"的波形。
    click / 丢字这类不连续在时域是一根尖刺，带限之后就是一段 2 ms 的
    能量凸起，而稳态的镜像残留是连续的、能量均匀的 —— 两者很好区分。"""
    n = 1 << int(np.ceil(np.log2(x.size)))
    X = np.fft.rfft(x, n)
    fr = np.fft.rfftfreq(n, 1.0 / MIC_SR)
    X[(fr < lo) | (fr > hi)] = 0
    return np.fft.irfft(X, n)[:x.size]


def report(tag, r, freq):
    print("\n===== %s =====" % tag)
    if r is None:
        return
    print("主音 f0=%.0f Hz 电平      %7.1f dBFS(相对满量程), 总 RMS %6.1f dB" %
          (freq, r["base"], r["rms"]))
    hs = "  ".join("H%d %+5.1f" % (k, v) for k, v in r["harm"].items())
    print("谐波（相对 f0）           " + hs)
    if r["harm"]:
        thd = 10 * np.log10(sum(10 ** (v / 10.0) for v in r["harm"].values()) + 1e-20)
        print("THD (2~8 次合计)          %+6.1f dB" % thd)
    print("12~22 kHz 宽带（相对 f0） %+6.1f dB" % r["hf"])
    print("冲击（12~22k 短时尖峰）   %d 次，峰值/中位 %.1f×" % (r["clicks"], r["peak_ratio"]))


def main():
    freq = 1000.0
    if "--freq" in sys.argv:
        freq = float(sys.argv[sys.argv.index("--freq") + 1])
    do_ref = "--ref" in sys.argv
    port = _autoport()
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]

    mic, mic_name = _pick(lambda n: "麦克风" in n, "max_input_channels")
    cab, cab_name = _pick(lambda n: "CABLE Input" in n, "max_output_channels")
    spk, spk_name = _pick(lambda n: "扬声器" in n and "CABLE" not in n,
                          "max_output_channels")
    print("麦克风     idx=%s %s" % (mic, mic_name))
    print("CABLE Input idx=%s %s" % (cab, cab_name))
    if do_ref:
        print("对照扬声器 idx=%s %s" % (spk, spk_name))
    if mic is None:
        print("RESULT: INCONCLUSIVE 没有麦克风")
        return 2

    # ---------------- 1) 静音段：只有链路在跑，没有声音 ----------------
    bridge = subprocess.Popen(
        [sys.executable, "-u", os.path.join(HERE, "fpga_audio_bridge.py"),
         "--port", port],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print("\n桥接程序 pid=%d，等 2.5 s…" % bridge.pid)
    time.sleep(2.5)
    y_sil = record(mic, 1.5)
    report("静音（桥接在跑、不播放）", analyze(y_sil, freq, "sil"), freq)

    # ---------------- 2) CABLE → 桥接 → 喇叭 的 1 kHz ----------------
    res = {}

    def worker():
        try:
            play_tone(cab, freq, 0.8, TONE_SEC)
        except Exception as e:
            print("播放失败：%r" % (e,))

    tw = threading.Thread(target=worker, daemon=True)
    tw.start()
    y = record(mic, LEAD_SEC + TONE_SEC + TAIL_SEC)
    tw.join(timeout=TONE_SEC + 5)
    time.sleep(0.3)
    bridge.terminate()
    blog = ""
    try:
        blog = bridge.stdout.read().decode("utf-8", "replace")
    except Exception:
        pass
    print("\n--- 桥接程序日志（最后 6 行）---")
    for ln in blog.splitlines()[-6:]:
        print("   " + ln[:110])

    i0 = int(LEAD_SEC * MIC_SR)
    tone_seg = y[i0:i0 + int(5.0 * MIC_SR)]
    res["dut"] = analyze(tone_seg, freq, "dut")
    report("CABLE→桥接→FPGA→MCU→喇叭", res["dut"], freq)

    # ---------------- 3) 可选：PC 扬声器同音量对照 ----------------
    if do_ref and spk is not None:
        def worker2():
            try:
                play_tone(spk, freq, 0.2, TONE_SEC)
            except Exception as e:
                print("对照播放失败：%r" % (e,))
        tw2 = threading.Thread(target=worker2, daemon=True)
        tw2.start()
        y2 = record(mic, LEAD_SEC + TONE_SEC + TAIL_SEC)
        tw2.join(timeout=TONE_SEC + 5)
        res["ref"] = analyze(y2[int(LEAD_SEC * MIC_SR):int((LEAD_SEC + 5.0) * MIC_SR)],
                             freq, "ref")
        report("PC 扬声器（对照，用同一支麦克风）", res["ref"], freq)
        if res["dut"] and res["ref"]:
            for key, name in (("harm", "THD"), ("hf", "12~22k 宽带")):
                if key == "harm":
                    a = 10 * np.log10(sum(10 ** (v / 10) for v in res["dut"]["harm"].values()) + 1e-20)
                    b = 10 * np.log10(sum(10 ** (v / 10) for v in res["ref"]["harm"].values()) + 1e-20)
                else:
                    a, b = res["dut"]["hf"], res["ref"]["hf"]
                print("   %-12s DUT %+6.1f dB / 对照 %+6.1f dB / 差 %+5.1f dB" %
                      (name, a, b, a - b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
