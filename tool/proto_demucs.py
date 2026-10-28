# -*- coding: utf-8 -*-
"""试 demucs：它能不能把这首合成二重唱的人声干净地拎出来。"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "host")))

import bench_split as B

print("生成合成二重唱 ...", flush=True)
mix, gt = B.make_duet()
# 取一段有人声的 24 秒（男独唱 + 女独唱 + 齐唱各 8 秒）
a0, a1 = int(8 * B.SR), int(32 * B.SR)
seg = mix[a0:a1]
print("测试片段 %.1f 秒" % (len(seg) / B.SR), flush=True)

import torch
print("torch", torch.__version__, "线程", torch.get_num_threads(), flush=True)
from demucs.pretrained import get_model
from demucs.apply import apply_model

t0 = time.time()
model = get_model("htdemucs")
model.eval()
print("模型载入 %.1f s；源：%s" % (time.time() - t0, model.sources), flush=True)

wav = torch.from_numpy(seg.T.copy()).float()          # (2, n)
ref = wav.mean(0)
wav_n = (wav - ref.mean()) / (ref.std() + 1e-8)

t0 = time.time()
with torch.no_grad():
    out = apply_model(model, wav_n[None], device="cpu", progress=True)[0]
out = out * ref.std() + ref.mean()
dt = time.time() - t0
print("分离用时 %.1f s（%.1f 倍实时）" % (dt, (len(seg) / B.SR) / dt), flush=True)

names = model.sources
vs = names.index("vocals") if "vocals" in names else 0
voc = out[vs].numpy().T                                # (n, 2)
acc = out.sum(0).numpy().T - voc

gt_voc = (gt["male"][a0:a1] + gt["female"][a0:a1]) * 0.55
gt_acc = np.stack([0.9 * gt["acc_l"][a0:a1], 0.9 * gt["acc_r"][a0:a1]], axis=1)

print("\n--- demucs 结果 ---")
print("人声 SI-SDR   : %+6.2f dB" % B.si_sdr(voc[:, 0], gt_voc))
print("伴奏 SI-SDR   : %+6.2f dB" % B.si_sdr(acc[:, 0], gt_acc[:, 0]))
print("人声泄漏到伴奏: %+6.2f dB（越低越好）"
      % B.si_sdr(voc[:, 0], gt_acc[:, 0] - gt_acc[:, 0]))

np.save(os.path.join(HERE, "_demucs_voc.npy"), voc.astype(np.float32))
np.save(os.path.join(HERE, "_demucs_acc.npy"), acc.astype(np.float32))
print("已存 tool/_demucs_voc.npy / _demucs_acc.npy")
