# -*- coding: utf-8 -*-
"""bench_stream.py —— 流式分离 vs 离线整段 的差距量化。

为什么必须测这个
----------------
界面上的"计算机本地模型（demucs）"走的是 local_stream.StreamSeparator，
它把音频切成 block_s 一块、每块**独立**跑一次 demucs。而 htdemucs 的
训练段长是 7.8 s —— 切小了模型看不到足够上下文，这是设计上已知的代价。

但还有三个不该有的代价，本脚本专门量它们：

  1. 块间随机性：STD 档 shifts=2 会在每次推理前加**随机**时间平移。
     每块随机数不同 → 同一段音乐在两块里的分离结果不同 → 听感"忽有忽无"。
  2. 块间增益跳变：local_ai.separate 内部按块 std 归一化，
     块与块的电平基准不同 → 人声音量台阶。
  3. 上下文不足：输入 5 s < 训练 7.8 s。

判据（全部与离线整段比）：
  err_db     流式人声轨与离线人声轨的差 / 离线人声轨 —— 越小越好
  env_r      逐 0.5 s 能量包络的相关系数 —— 越接近 1 越好（<0.9 = 能听出起伏）
  env_max_db 逐帧电平的最大偏差
  bleed_db   伴奏段人声轨残留（与前奏段比）

用法：
    python tool/bench_stream.py --secs 45
    python tool/bench_stream.py --secs 45 --block 1.0 --ctx 2.0 --quality fast
"""
import argparse
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
BENCH = os.path.join(ROOT, "tool", "logs", "sep_bench")
DEFAULT_SRC = r"C:/Users/Administrator/Downloads/老狼&王婧_想把我唱给你听.flac"


def load_src(src, secs, start=0.0):
    import soundfile as sf
    i = sf.info(src)
    x, sr = sf.read(src, start=int(start * i.samplerate),
                    frames=int(secs * i.samplerate) if secs else -1,
                    dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return x[:, :2].copy(), int(sr)


def env(x, sr, win=0.5):
    m = x.mean(axis=1) if x.ndim == 2 else x
    n = max(1, int(win * sr))
    k = len(m) // n
    r = np.sqrt(np.mean(m[:k * n].reshape(k, n).astype(np.float64) ** 2, axis=1) + 1e-30)
    return 20 * np.log10(r + 1e-12)


def db(v):
    return 20 * np.log10(max(float(v), 1e-12))


def run_stream(x, sr, block, ctx, quality, device, chunk=0.1):
    """用 local_stream 逐 100 ms 喂，模拟实时。返回 (voc, acc, 耗时)。"""
    import local_stream
    sep = local_stream.StreamSeparator(sr, device=device, quality=quality,
                                       block_s=block, ctx_s=ctx,
                                       progress=lambda s: None)
    if not sep.available:
        raise SystemExit("流式不可用: %s" % sep.why)
    step = int(chunk * sr)
    vs, as_ = [], []
    t0 = time.time()
    for i in range(0, len(x), step):
        v, a = sep.push(x[i:i + step])
        if v.size:
            vs.append(v)
            as_.append(a)
    v, a = sep.flush()
    if v.size:
        vs.append(v)
        as_.append(a)
    dt = time.time() - t0
    voc = np.concatenate(vs) if vs else np.zeros((0, 2), np.float32)
    acc = np.concatenate(as_) if as_ else np.zeros((0, 2), np.float32)
    n = min(len(voc), len(x))
    return voc[:n], acc[:n], dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--secs", type=float, default=45.0)
    ap.add_argument("--start", type=float, default=30.0,
                    help="从第几秒开始（默认 30，避开纯前奏、直取人声进来的地方）")
    ap.add_argument("--block", type=float, default=1.0)
    ap.add_argument("--ctx", type=float, default=2.0)
    ap.add_argument("--quality", default="std")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--ref", default="", help="复用已存的离线结果 tag（省一次推理）")
    a = ap.parse_args()

    x, sr = load_src(a.src, a.secs, a.start)
    print("素材 %.1f s @ %d Hz（从 %.1f s 起）" % (len(x) / sr, sr, a.start))

    # ---- 参考：离线整段 ----
    os.makedirs(BENCH, exist_ok=True)
    ref_npz = os.path.join(BENCH, "_ref_offline.npz")
    if a.ref and os.path.isfile(os.path.join(BENCH, a.ref, "vocals.wav")):
        import soundfile as sf
        ref_v, _ = sf.read(os.path.join(BENCH, a.ref, "vocals.wav"),
                           dtype="float32", always_2d=True)
        ref_a, _ = sf.read(os.path.join(BENCH, a.ref, "acc.wav"),
                           dtype="float32", always_2d=True)
        n = min(len(ref_v), len(x))
        ref_v, ref_a = ref_v[:n], ref_a[:n]
        print("参考：复用 %s" % a.ref)
    else:
        import local_ai
        t0 = time.time()
        ref_v, ref_a, info = local_ai.separate(x, sr, device=a.device, quality=a.quality)
        print("参考：离线整段 %.1f s（RTF %.2f）" % (time.time() - t0,
                                                  (time.time() - t0) / (len(x) / sr)))
    n = min(len(ref_v), len(x))

    # ---- 被测：流式 ----
    sv, sa, dt = run_stream(x, sr, a.block, a.ctx, a.quality, a.device)
    n2 = min(n, len(sv))
    sv, sa = sv[:n2], sa[:n2]
    rv, ra = ref_v[:n2], ref_a[:n2]

    e_ref = env(rv, sr)
    e_str = env(sv, sr)
    k = min(len(e_ref), len(e_str))
    e_ref, e_str = e_ref[:k], e_str[:k]

    # 误差能量
    err = sv - rv
    err_db = db(np.sqrt(np.mean(err.astype(np.float64) ** 2)) /
                (np.sqrt(np.mean(rv.astype(np.float64) ** 2)) + 1e-12))
    # 包络一致性
    ec = e_str - e_str.mean()
    rc = e_ref - e_ref.mean()
    r = float(np.corrcoef(ec, rc)[0, 1]) if ec.std() > 1e-9 else float("nan")
    # 只看"有声音"的帧（静音帧的 dB 差没意义）
    live = e_ref > (e_ref.max() - 40)
    env_max = float(np.abs(e_str[live] - e_ref[live]).max()) if live.any() else float("nan")
    env_rms = float(np.sqrt(np.mean((e_str[live] - e_ref[live]) ** 2))) if live.any() else float("nan")

    # 前 10% 最安静段（近似伴奏段）的 bleed
    idx = np.argsort(e_ref)[:max(1, k // 10)]
    bleed_ref = float(np.mean(e_ref[idx]))
    bleed_str = float(np.mean(e_str[idx]))

    print()
    print("%-28s %12s" % ("", "value"))
    print("-" * 42)
    print("%-28s %12.1f" % ("流式 vs 离线 误差 dB", err_db))
    print("%-28s %12.3f" % ("逐 0.5s 包络相关系数", r))
    print("%-28s %12.1f" % ("包络最大偏差 dB", env_max))
    print("%-28s %12.1f" % ("包络 RMS 偏差 dB", env_rms))
    print("%-28s %12.1f" % ("最静段 离线人声 dB", bleed_ref))
    print("%-28s %12.1f" % ("最静段 流式人声 dB", bleed_str))
    print("%-28s %12.2f" % ("流式耗时 / 音频时长", dt / (len(x) / sr)))
    print()
    print("判读：误差 < -20 dB 且 包络相关 > 0.99 才算「流式听不出差别」；"
          "包络相关 < 0.9 就是能听出的忽有忽无")


if __name__ == "__main__":
    main()
