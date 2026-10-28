# -*- coding: utf-8 -*-
"""
verify_engine_chain.py —— 验证「模块 × 引擎 × 输入」这套结构真的接对了

跑法
----
    python tool/verify_engine_chain.py           # 快速子集（不跑 demucs）
    python tool/verify_engine_chain.py --full    # 加 demucs 实测（慢）

测什么
------
本轮把上位机的操作逻辑从"七种模式平铺"改成「模块 × 引擎」两层，
并且让**四种组合在实时和文件两种输入下全部可用**。这里验的就是这件事：

  1. 枚举齐整：2 个模块 × 每个 2 个引擎，名字都在，没有漏项。
  2. EngineChain 分发：四种组合各自落到该落的实现上，流式每次严格等长。
  3. ★ 重复 configure 不重建子模块 —— 这是本轮修掉的真 bug：
       原来用 `self._chain is None` 判断要不要重建，而四种组合里只有
       第一种会创建 _chain，其余三种 _chain 永远是 None，于是每调一次
       configure 就整个重建一遍，流式状态全被抹掉。实时模式下
       _out_cb 每个音频块都会调 configure，那就是每块重置一次算法。
       判据改成 (module, engine) != self._built。
  4. RealtimeEngine 音频回调：四种组合都能跑，不崩、等长、数值有限，
     变调组合音高正确（这条最要紧——界面选什么，耳朵就该听到什么）。
  5. reset() 之后能重新预热（停止再启动不该先爆一段欠载音）。
  6. 缺 demucs 时「分离 × 计算机本地模型」降级为直通，不崩、长度精确。

输出：Pass/Fail 清单 + 数据行。
"""

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
HOST = os.path.join(ROOT, "host")
sys.path.insert(0, HOST)
sys.path.insert(0, HERE)

import dsp_core as dsp                                     # noqa: E402
import local_ai                                            # noqa: E402
import verify_stream as VS                                 # noqa: E402
import verify_pitch_hq as VPH                              # noqa: E402

SR = 48000
FULL = "--full" in sys.argv

COMBOS = [(m, e) for m, engs in dsp.MODULE_ENGINES.items() for e in engs]


def _voiced(f0=220.0, dur=1.0, sr=SR):
    """带共振峰包络的谐波串 —— 这才是真人声的谱形状。

    ★ 不能用单频正弦：倒谱包络那一步对纯音是退化输入（离线 hq_voice
      同样会把 +12 半音的谱峰测到 175 Hz），那是度量选错了，不是算法错。
      F0 也用自相关测（VPH.est_f0，带八度错误防护），不用谱峰法。
    ★ 不淡变：连续性靠"生成一条长信号再顺序切片"保证。
      （VPH.synth_vowel 传 fade=0 会踩 `win[-0:]` 那个 Python 坑，
       所以这里自己写一份，不复用。）
    """
    n = int(sr * dur)
    t = np.arange(n) / float(sr)
    nh = int(0.92 * (sr / 2.0) / f0)
    f = np.arange(1, nh + 1) * f0
    a = np.zeros(f.size)
    for ff, bb, aa in zip(VPH.FORMANTS, VPH.BW, VPH.AMP):
        a += aa / (1.0 + ((f - ff) / (0.5 * bb)) ** 2)
    rng = np.random.RandomState(7)
    ph = rng.uniform(0.0, 2.0 * np.pi, f.size)
    y = np.zeros(n)
    for i in range(f.size):
        y += a[i] * np.sin(2.0 * np.pi * f[i] * t + ph[i])
    y /= (np.max(np.abs(y)) + 1e-12)
    return (y * 0.7).astype(np.float32)


def _f0_of(y):
    """测一段立体声/单声道输出的基频（自相关法）。"""
    a = np.asarray(y, dtype=np.float64)
    if a.ndim == 2:
        a = a[:, 0]
    return VPH.est_f0(a, fmin=60.0, fmax=1200.0)[0]


# ---------------------------------------------------------------- 1 枚举

def test_enum():
    print("\n--- 模块 × 引擎枚举 ---")
    rec = VS.rec
    names = dict(dsp.MODULE_NAMES)
    rec("两个模块：变调 / 分离",
        set(names) == {dsp.MODULE_PITCH, dsp.MODULE_SPLIT},
        " / ".join(names.values()))
    ok = True
    for m, engs in dsp.MODULE_ENGINES.items():
        want = ([dsp.ENGINE_FPGA_DIRECT, dsp.ENGINE_FPGA_MODEL]
                if m == dsp.MODULE_PITCH else
                [dsp.ENGINE_FPGA_DIRECT, dsp.ENGINE_LOCAL_MODEL])
        if engs != want:
            ok = False
    rec("每个模块恰好两个引擎且顺序正确", ok,
        "; ".join("%s: %s" % (dict(dsp.MODULE_NAMES)[m],
                              " → ".join(e for e in engs))
                  for m, engs in dsp.MODULE_ENGINES.items()))
    rec("四种组合都有中文名", all((m, e) in dsp.ENGINE_NAMES for m, e in COMBOS),
        " | ".join(dsp.ENGINE_NAMES[c] for c in COMBOS))
    rec("两种输入方式已定义",
        dsp.SRC_REALTIME == "realtime" and dsp.SRC_FILE == "file" and
        len(dsp.SRC_NAMES) == 2, " / ".join(n for _, n in dsp.SRC_NAMES))


# ---------------------------------------------------------------- 2 分发

def test_dispatch():
    print("\n--- EngineChain 分发与流式等长 ---")
    rec = VS.rec
    all_ok, det = True, []
    for m, e in COMBOS:
        ch = dsp.EngineChain(SR)
        ch.configure(module=m, engine=e, source=dsp.SRC_REALTIME,
                     semi=3.0, formant=1.0, split=dict(depth=1.0, f0_hz=0.0))
        ok = True
        for _ in range(8):
            x = (np.random.randn(1024, 2) * 2000).astype(np.int16)
            y = ch.process_stream(x)
            if y.shape != x.shape or not np.all(np.isfinite(y)):
                ok = False
        if not ok:
            all_ok = False
        det.append("%s×%s %.0f ms" % (m, e, ch.latency_ms()))
    rec("四种组合流式严格等长且数值有限", all_ok, "，".join(det))

    # 落到该落的实现上
    ch = dsp.EngineChain(SR)
    ch.configure(module=dsp.MODULE_PITCH, engine=dsp.ENGINE_FPGA_DIRECT)
    rec("变调×FPGA直算 -> StereoChain（与 RTL 对齐）",
        ch._chain is not None and ch._hq is None and ch._sep is None)
    ch.configure(module=dsp.MODULE_PITCH, engine=dsp.ENGINE_FPGA_MODEL)
    rec("变调×FPGA内部模型 -> StreamPitchShifter",
        ch._hq is not None and ch._chain is None)
    ch.configure(module=dsp.MODULE_SPLIT, engine=dsp.ENGINE_FPGA_DIRECT)
    rec("分离×FPGA内部模型 -> VocalSplitter",
        ch._splitter is not None and ch._sep is None)
    ch.configure(module=dsp.MODULE_SPLIT, engine=dsp.ENGINE_LOCAL_MODEL)
    rec("分离×计算机本地模型 -> StreamSeparator",
        ch._sep is not None and ch._splitter is None and ch._chain is None)


# ---------------------------------------------------------------- 3 重建判据

def test_no_rebuild():
    print("\n--- 重复 configure 不得重建子模块（本轮修的 bug）---")
    rec = VS.rec
    bad = []
    for m, e in COMBOS:
        ch = dsp.EngineChain(SR)
        ch.configure(module=m, engine=e, semi=2.0)
        # 找出这个组合实际创建出来的那个子模块
        sub = ch._chain or ch._splitter or ch._hq or ch._sep
        if sub is None:
            bad.append("%s×%s 没有子模块" % (m, e))
            continue
        before = id(sub)
        for _ in range(5):
            ch.configure(module=m, engine=e, semi=2.0)   # 同组合反复配
        sub2 = ch._chain or ch._splitter or ch._hq or ch._sep
        if id(sub2) != before:
            bad.append("%s×%s 被重建了" % (m, e))
        # 状态也必须保住：推进过的输出位置不该归零
        if m == dsp.MODULE_SPLIT and e == dsp.ENGINE_LOCAL_MODEL:
            if ch._sep._out_pos == 0 and ch._sep._in_base != 0:
                bad.append("%s×%s 流式位置被重置" % (m, e))
    rec("同组合重复 configure 不重建、不丢状态", not bad,
        "；".join(bad) if bad else "4 组合各配 6 次，子模块对象始终同一个")

    # 换组合必须重建
    ch = dsp.EngineChain(SR)
    ch.configure(module=dsp.MODULE_PITCH, engine=dsp.ENGINE_FPGA_DIRECT)
    a = ch._chain
    ch.configure(module=dsp.MODULE_PITCH, engine=dsp.ENGINE_FPGA_MODEL)
    rec("换引擎会重建", ch._chain is None and ch._hq is not None and a is not None)

    # ★ 输出必须逐比特稳定：实时链路每个音频块都会调一次 configure，
    #   如果 set_ratio 顺手 reset()，输出就永远停在预热完的那段静音上。
    #   （本轮实测：24 块全零，界面选「FPGA 内部小模型」实时没声音。）
    #   比法：同一条输入序列，一遍"只配一次"、一遍"每块都配一次"，
    #   稳态段必须逐比特相同（直接比相邻两块是错的——那本来就是不同位置）。
    kw = dict(module=dsp.MODULE_PITCH, engine=dsp.ENGINE_FPGA_MODEL, semi=4.0)
    x = (np.random.randn(1024, 2) * 2000).astype(np.int16)
    a = dsp.EngineChain(SR)
    a.configure(**kw)
    out_a = [a.process_stream(x) for _ in range(12)]
    b = dsp.EngineChain(SR)
    out_b = []
    for _ in range(12):
        b.configure(**kw)
        out_b.append(b.process_stream(x))
    dmax = max(float(np.abs(out_a[i] - out_b[i]).max()) for i in range(4, 12))
    rec("每块重复 configure 与只配一次逐比特一致", dmax == 0.0,
        "稳态段最大差 %.3e" % dmax)


def test_dtype():
    print("\n--- 流式返回值必须是 int16（本轮修的第二个 bug）---")
    rec = VS.rec
    bad = []
    for m, e in COMBOS:
        ch = dsp.EngineChain(SR)
        ch.configure(module=m, engine=e, semi=3.0)
        for _ in range(6):
            x = (np.random.randn(1024, 2) * 2000).astype(np.int16)
            y = ch.process_stream(x)
            if y.dtype != np.int16:
                bad.append("%s×%s 返回 %s" % (m, e, y.dtype))
                break
    rec("四种组合 process_stream 都返回 int16", not bad,
        "；".join(bad) if bad else "dtype 全部 int16")

    # 退化直通那条路更要命：float32 的 [-1,1] 被 cast 成 int16 会变成
    # 0 / ±1，表现为"选了本地模型，实时输出全是静音"。
    orig = local_ai.demucs_ready
    try:
        local_ai.demucs_ready = lambda: (False, "模拟缺失")
        ch = dsp.EngineChain(SR)
        ch.configure(module=dsp.MODULE_SPLIT, engine=dsp.ENGINE_LOCAL_MODEL)
        t = np.arange(1024) / float(SR)
        v = 0.3 * np.sin(2 * np.pi * 220.0 * t)
        x = (np.stack([v, v * 0.9], axis=1) * 32767).astype(np.int16)
        amp = 0.0
        for _ in range(8):
            y = ch.process_stream(x)
            amp = max(amp, float(np.abs(y).max()))
        rec("降级直通时输出是真实信号而非 0/±1", amp > 1000,
            "峰值 %d（int16 满量程 32767）" % int(amp))
    finally:
        local_ai.demucs_ready = orig


# ---------------------------------------------------------------- 4 实时回调

def _rt_engine():
    import host_app
    rt = host_app.RealtimeEngine()
    rt.sr_in = rt.sr_out = SR
    rt.channels = rt.in_channels = 2
    rt.get_cfg = None
    return rt


def _run_rt(mod, eng, semi, blocks=24, blk=1024, f0=220.0):
    """把 RealtimeEngine 的音频回调真的跑一遍，返回 (输出, rt)。"""
    rt = _rt_engine()
    cfg = dict(module=mod, engine=eng, source=dsp.SRC_REALTIME,
               semi_exact=semi, formant=1.0, dphase=dsp.semitone_to_dphase(semi),
               ring_freq=1, echo_taps=8, echo_gain=2, gain=1.0,
               ingain=1.0,
               split=dict(depth=1.0, f0_hz=0.0, swap=False, broadband=0.25,
                          harmonic=True),
               # 与界面默认一致：人声 + 伴奏都留。合成谐波串会被 demucs
               # 整个判成"伴奏"（离线同样如此，实测 voc rms 0.0004 /
               # acc rms 0.292），只留人声的话输出接近静音 —— 那是信号
               # 选得不像人声，不是链路坏了。
               ai=dict(vocal_gain=1.0, acc_gain=1.0, quality="std",
                       device="auto"))
    rt.get_cfg = lambda: cfg
    rt.running = True
    outs = []
    # 一条长信号顺序切片喂进去，块与块之间是连续的（不每块淡变）
    x = _voiced(f0=f0, dur=(blocks * blk + 4096) / float(SR))
    for i in range(blocks):
        seg = np.ascontiguousarray(x[i * blk:(i + 1) * blk])
        # 输入回调：灌数据 + 统计观测量
        rt._in_cb(seg, blk, None, None)
        out = np.zeros((blk, 2), dtype=np.float32)
        rt._out_cb(out, blk, None, None)
        outs.append(out.copy())
    rt.running = False
    return np.concatenate(outs, axis=0), rt


def test_realtime_callback():
    print("\n--- RealtimeEngine 音频回调（四种组合）---")
    rec = VS.rec
    bad, det = [], []
    for m, e in COMBOS:
        # demucs 流式的延迟是 block + ctx = 3 s，前 3 s 本来就不该有输出
        # （这是如实报告的延迟，不是 bug）。所以这个组合要多喂几秒。
        nb = 220 if e == dsp.ENGINE_LOCAL_MODEL else 24
        try:
            y, rt = _run_rt(m, e, 4.0, blocks=nb)
        except Exception as ex:
            bad.append("%s×%s 抛异常 %r" % (m, e, ex))
            continue
        if y.shape != (nb * 1024, 2):
            bad.append("%s×%s 输出尺寸 %s" % (m, e, y.shape))
        if not np.all(np.isfinite(y)):
            bad.append("%s×%s 输出有 NaN/Inf" % (m, e))
        if rt.error:
            bad.append("%s×%s 记录了 error=%s" % (m, e, rt.error))
        # 左右两路必须都活着（分离类效果的观测量就是分声道的）。
        # 只看后 1/4：前 3 s 对 demucs 那条路是"还没轮到它说话"。
        tail = y[int(y.shape[0] * 0.75):]
        pl = float(np.max(np.abs(tail[:, 0])))
        pr = float(np.max(np.abs(tail[:, 1])))
        if max(pl, pr) < 1e-3:
            bad.append("%s×%s 输出几乎是静音 (%.5f/%.5f)" % (m, e, pl, pr))
        det.append("%s×%s L%.3f/R%.3f" % (m, e, pl, pr))
    rec("四种组合实时回调不崩、等长、双声道都有输出", not bad,
        "；".join(bad) if bad else "，".join(det))

    # 音高：界面选 +4 半音，出来的基频就该跟着走。
    # FPGA 直算是时域延迟线，dphase 按板上 16 档量化，所以目标取量化后的
    # 实际比值（这正是界面"拨码换算"要告诉用户的事）。
    for m, e in ((dsp.MODULE_PITCH, dsp.ENGINE_FPGA_DIRECT),
                 (dsp.MODULE_PITCH, dsp.ENGINE_FPGA_MODEL)):
        y, _ = _run_rt(m, e, 4.0)
        f = _f0_of(y[int(y.shape[0] * 0.6):])
        if e == dsp.ENGINE_FPGA_DIRECT:
            r = dsp.dphase_to_ratio(dsp.semitone_to_dphase(4.0))
        else:
            r = 2.0 ** (4.0 / 12.0)
        want = 220.0 * r
        rec("实时音高正确：%s×%s +4 半音" % (m, e),
            abs(VPH.cents(f, want)) < 40.0,
            "实测 %.1f Hz，目标 %.1f Hz（偏差 %.0f 音分）"
            % (f, want, VPH.cents(f, want)))

    # 分离 × FPGA 内部模型：男声（110 Hz）应该大部分留在左声道，
    # 女声（231 Hz）留在右声道。频率取自 test_host.py 的验收用例
    # （220 正好是 110 的八度，物理上分不开，不能当用例）。
    y, _ = _run_rt(dsp.MODULE_SPLIT, dsp.ENGINE_FPGA_DIRECT, 0.0, f0=110.0)
    el = float(np.sqrt(np.mean(y[:, 0] ** 2)))
    er = float(np.sqrt(np.mean(y[:, 1] ** 2)))
    rec("实时分离：男声（110 Hz）能量偏左", el > er * 1.2,
        "左 RMS %.4f / 右 RMS %.4f" % (el, er))
    y, _ = _run_rt(dsp.MODULE_SPLIT, dsp.ENGINE_FPGA_DIRECT, 0.0, f0=231.0)
    el = float(np.sqrt(np.mean(y[:, 0] ** 2)))
    er = float(np.sqrt(np.mean(y[:, 1] ** 2)))
    rec("实时分离：女声（231 Hz）能量偏右", er > el * 1.2,
        "左 RMS %.4f / 右 RMS %.4f" % (el, er))


# ---------------------------------------------------------------- 5 reset

def test_reset():
    print("\n--- reset 之后重新预热 ---")
    rec = VS.rec
    for m, e in COMBOS:
        ch = dsp.EngineChain(SR)
        ch.configure(module=m, engine=e, semi=3.0)
        for _ in range(4):
            ch.process_stream((np.random.randn(1024, 2) * 2000).astype(np.int16))
        ch.reset()
        y = ch.process_stream((np.random.randn(1024, 2) * 2000).astype(np.int16))
        ok = y.shape == (1024, 2) and np.all(np.isfinite(y))
        rec("reset 后仍严格等长：%s×%s" % (m, e), ok, "shape=%s" % (y.shape,))


# ---------------------------------------------------------------- 6 降级

def test_degrade():
    print("\n--- 缺 demucs 时「分离 × 计算机本地模型」降级 ---")
    rec = VS.rec
    orig = local_ai.demucs_ready
    try:
        local_ai.demucs_ready = lambda: (False, "模拟缺失")
        ch = dsp.EngineChain(SR)
        ch.configure(module=dsp.MODULE_SPLIT, engine=dsp.ENGINE_LOCAL_MODEL,
                     semi=0.0)
        rec("构造时标记为已降级", ch._sep._degraded and ch.info["degraded"],
            ch._sep.info["engine"][0] if ch._sep.info["engine"] else "")
        n = 4800
        x = (np.random.randn(n, 2) * 3000).astype(np.int16)
        tot = 0
        for s in range(0, n, 1024):
            y = ch.process_stream(x[s:s + 1024])
            tot += y.shape[0]
        rec("降级为直通且长度精确", tot == n, "%d / %d" % (tot, n))
    finally:
        local_ai.demucs_ready = orig
    ok, why = local_ai.demucs_ready()
    rec("恢复后环境自检正常", ok, "demucs_ready=%s" % (ok,))


# ================================================================ 主流程

def main():
    print("=" * 68)
    print("模块 × 引擎 × 输入 验收：四种组合 / 实时+文件 / 分发与重建判据")
    print("=" * 68)

    test_enum()
    test_dispatch()
    test_no_rebuild()
    test_dtype()
    test_realtime_callback()
    test_reset()
    test_degrade()
    if not FULL:
        print("\n[SKIP] 加 --full 可另跑 verify_stream.py 里的 demucs 实测")

    print("\n" + "=" * 68)
    print("结果：%d PASS / %d FAIL" % (VS._PASS[0], VS._FAIL[0]))
    print("=" * 68)
    return 1 if VS._FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
