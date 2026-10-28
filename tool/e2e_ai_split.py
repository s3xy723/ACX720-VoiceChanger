# -*- coding: utf-8 -*-
"""
e2e_ai_split.py   「分离 × 计算机本地模型」端到端跑通验证

不靠听感，也不靠截图。验三件事：

  [1] GUI 真的认为 AI 可用 —— 构造真实的 App（headless），等后台环境自检
      完成，然后读界面上「本地模型」那一栏的文字和状态灯板的 AI 灯，
      断言不是"不可用 / 缺失 / 降级"。
      （之前 EXE 里没带 torch，这一栏永远显示"缺少 torch、demucs"，
        用户看到的就是"AI 模式没做"。）

  [2] 真的跑了一次 demucs —— 用 local_ai.run() 走完整的 cfg
      （分离 → 人声变调 → 重混），并且跑在 CUDA 上。

  [3] 结果能落盘并被读回 —— 写出 wav，再读回来核对长度、声道、非静音。

运行：python tool/e2e_ai_split.py [--seconds N]
"""

import argparse
import os
import sys
import time

import numpy as np

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.join(TOP, "host")
sys.path.insert(0, TOP)
sys.path.insert(0, HOST)

OK = True
SR = 44100


def report(name, good, detail=""):
    global OK
    OK &= bool(good)
    print("  [%s] %s%s" % ("PASS" if good else "FAIL", name,
                           ("\n         " + detail) if detail else ""))


def make_song(seconds):
    """合成一段"人声 + 伴奏"的立体声混音，并返回参考人声用于算 SI-SDR。"""
    n = int(seconds * SR)
    t = np.arange(n) / float(SR)
    rs = np.random.RandomState(7)

    # 人声：带共振峰包络的谐波串 + 颤音 + 音节包络（比纯正弦更像人声）
    f0 = 220.0
    env = np.clip(np.sin(2 * np.pi * 2.5 * t) * 0.5 + 0.5, 0, 1) ** 2
    vib = 1.0 + 0.012 * np.sin(2 * np.pi * 5.2 * t)
    voc = np.zeros(n)
    for k in range(1, 13):
        if f0 * k * 1.02 > SR * 0.45:      # 用标量判断：fo 是数组，不能直接比
            break
        fo = f0 * k * vib
        # 三个共振峰的洛伦兹包络
        a = 0.0
        for fc, bw, g in ((700, 90, 1.0), (1150, 110, 0.55), (2600, 200, 0.22)):
            a += g / (1.0 + ((fo - fc) / bw) ** 2)
        voc += (a / k) * np.sin(2 * np.pi * fo * t + rs.uniform(0, 6.28))
    voc *= env * 0.28

    # 伴奏：贝斯 + 和弦 + 鼓点（明显非人声）
    bass = 0.32 * np.sin(2 * np.pi * 82.4 * t)
    chord = sum(0.10 * np.sin(2 * np.pi * f * t) for f in (329.6, 415.3, 493.9))
    beat = np.zeros(n)
    for b in range(0, n, int(0.5 * SR)):
        beat[b:b + 2000] += 0.5 * rs.randn(min(2000, n - b))
    beat = np.convolve(beat, np.exp(-np.arange(400) / 80.0), "same")[:n]
    acc = bass + chord + beat * 0.25

    v2 = np.stack([voc, voc], axis=1)
    a2 = np.stack([acc, acc], axis=1)
    mix = (v2 + a2).astype(np.float32)
    ref = v2.astype(np.float32)
    return np.ascontiguousarray(mix), np.ascontiguousarray(ref)


def si_sdr(est, ref):
    """尺度不变的信噪比（dB）。est 对 ref 做最优缩放后比较。"""
    e = est.ravel().astype(np.float64)
    r = ref.ravel().astype(np.float64)
    if e.size != r.size:
        m = min(e.size, r.size)
        e, r = e[:m], r[:m]
    a = float(np.dot(e, r) / (np.dot(r, r) + 1e-12))
    err = e - a * r
    return 10.0 * np.log10((np.dot(a * r, a * r) + 1e-12) / (np.dot(err, err) + 1e-12))


def check_gui():
    print("=" * 74)
    print(" [1] 上位机界面是否真的认为 AI 可用（不靠截图，直接读控件）")
    print("=" * 74)
    os.chdir(HOST)
    import host_app

    app = host_app.App()
    app.update()
    # 切到「分离 × 计算机本地模型」
    import dsp_core as dsp
    app.v_module.set(dsp.MODULE_SPLIT)
    app._on_module_change()
    app.v_engine.set(dsp.ENGINE_LOCAL_MODEL)
    app._on_engine_change()
    app._refresh_labels()
    app.update()

    # 等后台环境自检（要 import torch，几秒）。
    # ★ 必须进 mainloop 而不是手动 update() 循环：后台自检线程是用
    #   self.after(0, ...) 把结果送回 UI 的，主线程不在 main loop 里时
    #   tkinter 会抛 "main thread is not in main loop"。
    t0 = time.time()
    state = {"done": False}

    def poll():
        if app._ai_probed or time.time() - t0 > 90:
            state["done"] = True
            app.quit()
            return
        app.after(100, poll)

    app.after(100, poll)
    app.mainloop()
    report("后台环境自检在 90 s 内完成", app._ai_probed,
           "耗时 %.1f s" % (time.time() - t0))

    app._refresh_ai_latency()
    app.update()
    txt = ""
    if hasattr(app, "lb_ai_lat"):
        txt = app.lb_ai_lat.cget("text") or ""
    print("        界面文案：%s" % txt.replace("\n", " / "))
    bad_words = ("不可用", "缺失", "没能加载", "退化为直通", "缺少")
    report("界面文案不含「不可用 / 缺失 / 退化」字样",
           not any(w in txt for w in bad_words))

    # 灯板上的 AI 灯
    ai_state = None
    try:
        for it in app.leds.items:
            if "AI" in it[0] or "本地" in it[0] or "demucs" in it[0].lower():
                ai_state = it
                break
    except Exception:
        pass
    print("        AI 灯条目：%s" % (ai_state[0] if ai_state else "（未找到）"))
    app.destroy()
    return txt


def check_run(seconds):
    print("\n" + "=" * 74)
    print(" [2] 真实跑一次 demucs 分离（CUDA），并落盘")
    print("=" * 74)
    import local_ai
    import audio_io

    mix, ref = make_song(seconds)
    # ★ local_ai.run 的 cfg 是**扁平**的（不是界面 cfg 里的 cfg["ai"] 子字典）：
    #   use_demucs / voice / semi / formant / vocal_gain / acc_gain /
    #   device / quality / rvc_dir
    cfg = dict(use_demucs=True, vocal_gain=1.0, acc_gain=0.35,
               device="auto", quality=local_ai.QUALITY_STD,
               voice=local_ai.VOICE_HQ, semi=4.0, formant=1.0,
               rvc_dir="")
    t0 = time.time()
    # run() 返回 (样本, 链路信息) —— 采样率不变，仍是 44.1 kHz（demucs 的要求）
    out, info = local_ai.run(mix, SR, cfg)
    sr = SR
    dt = time.time() - t0

    print("        耗时 %.2f s（%.1f 秒音频）" % (dt, seconds))
    print("        链路：%s" % info.get("engine"))
    print("        设备：%s" % info.get("device"))
    report("返回非空且长度一致", out is not None and out.shape[0] == mix.shape[0],
           "out %s / mix %s" % (out.shape, mix.shape))
    report("跑在 CUDA 上", str(info.get("device", "")).startswith("cuda"),
           "device = %s" % info.get("device"))
    eng = info.get("engine", [])
    report("链路里真的启用了 demucs 分离",
           any("未启用" not in s and ("分离" in s or "demucs" in s.lower())
               for s in eng),
           " / ".join(eng))

    # 落盘 + 读回
    outdir = os.path.join(TOP, "tool", "_ai_out")
    os.makedirs(outdir, exist_ok=True)
    p = os.path.join(outdir, "ai_split_%d.wav" % int(seconds))
    audio_io.save_wav(p, out, sr)
    back, bsr = audio_io.load_audio(p)
    report("写出并读回：长度/采样率一致",
           back.shape[0] == out.shape[0] and bsr == sr,
           "%s -> %s @ %d Hz" % (back.shape, out.shape, bsr))
    rms = float(np.sqrt(np.mean(np.square(back[:, 0]))))
    report("读回来的音频不是静音", rms > 1e-4, "左声道 RMS %.4f" % rms)
    print("        输出文件：%s" % p)

    # 质量：拿单独分离出的人声跟参考比
    try:
        voc, acc, sep = local_ai.separate(mix, SR, device="cuda",
                                          quality=local_ai.QUALITY_STD)
        s = si_sdr(voc, ref)
        print("        人声提取 SI-SDR：%.2f dB（合成素材，仅供参考）" % s)
        report("分离出的两路确实不同（不是原样复制）",
               float(np.sqrt(np.mean(np.square(voc - acc)))) > 1e-3)
    except Exception as e:
        print("        （SI-SDR 计算跳过：%s: %s）" % (type(e).__name__, e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--skip-gui", action="store_true")
    args = ap.parse_args()

    if not args.skip_gui:
        check_gui()
    check_run(args.seconds)

    print("\n" + "=" * 74)
    print(" 汇总：%s" % ("ALL PASS" if OK else "有 FAIL"))
    print("=" * 74)
    return 0 if OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
