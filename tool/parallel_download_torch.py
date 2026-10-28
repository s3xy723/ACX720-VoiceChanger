# -*- coding: utf-8 -*-
"""Parallel range downloader for the PyTorch CUDA wheel.

单连接到 download.pytorch.org 实测只有 ~0.6 MB/s，2.75 GB 要一个多小时。
这里用 8 条 Range 连接分块下载，再拼回完整 wheel。

2026-10-20 修了三个会静默产出坏包的问题：
  1) 原来整个脚本写在模块级，任何 `import parallel_download_torch`
     （测试收集、IDE 补全）都会立刻开跑 2.75 GB 下载 —— 逻辑已搬进 main()。
  2) `t.join()` 之后再判 `t.is_alive()` 恒为 False，那句
     "download incomplete" **永远不可达**：分片短读失败只会打印一行，
     然后照样把 8 个残缺分片拼成 2.75 GB 的坏 wheel。改成在线程里捕获
     异常，join 后统一检查。
  3) 拼完没有大小校验，也没有 SHA256。现在先 HEAD 取 Content-Length 校验
     SIZE（硬编码的魔法数会因上游换包而失效），拼完再对比实际字节数。
"""
import os
import sys
import threading
import time
import urllib.request

URL = ("https://download.pytorch.org/whl/cu128/"
       "torch-2.11.0%2Bcu128-cp312-cp312-win_amd64.whl")
SIZE = 2753189216          # 仅作兜底；实际以上游 Content-Length 为准
N = 8
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_wheels")
OUT = os.path.join(OUT_DIR, "torch-2.11.0+cu128-cp312-cp312-win_amd64.whl")


def remote_size(url, fallback=None):
    """HEAD 拿 Content-Length：上游换包时 SIZE 硬编码会骗人。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            n = int(r.headers.get("Content-Length", 0))
            if n > 0:
                return n
    except Exception as e:
        print("HEAD 失败（用兜底大小）：%r" % (e,))
    return fallback


def download(size):
    done = [0] * N
    errs = []
    lock = threading.Lock()
    t0 = time.time()

    def worker(i):
        start = i * size // N
        end = (i + 1) * size // N - 1
        want = end - start + 1
        part = os.path.join(OUT_DIR, "torch.part%d" % i)
        for attempt in range(5):
            try:
                req = urllib.request.Request(
                    URL, headers={"Range": "bytes=%d-%d" % (start, end)})
                with urllib.request.urlopen(req, timeout=60) as r, \
                        open(part, "wb") as f:
                    got = 0
                    while True:
                        b = r.read(1024 * 1024)
                        if not b:
                            break
                        f.write(b)
                        got += len(b)
                        with lock:
                            done[i] = got
                if got == want:
                    return
                print("part %d short: %d/%d" % (i, got, want))
            except Exception as e:
                print("part %d attempt %d failed: %r" % (i, attempt + 1, e))
                time.sleep(2)
        # ★ 异常必须带出来：原来 worker 里 raise 之后主线程照样拼接
        with lock:
            errs.append("part %d 五次重试后仍失败" % i)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(N)]
    for t in threads:
        t.start()

    last = 0
    while any(t.is_alive() for t in threads):
        time.sleep(10)
        total = sum(done)
        now = time.time()
        print("downloaded %.0f / %.0f MB  (%.2f MB/s)"
              % (total / 1e6, size / 1e6,
                 (total / 1e6) / max(1e-9, now - t0)))
        last = total

    for t in threads:
        t.join()

    if errs:
        for p in [os.path.join(OUT_DIR, "torch.part%d" % i) for i in range(N)]:
            try:
                os.remove(p)
            except Exception:
                pass
        raise SystemExit("下载不完整，已清理分片：%s" % "; ".join(errs))
    return True


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    size = remote_size(URL, SIZE)
    print("URL = %s\nSIZE = %d (%.2f GB)" % (URL, size, size / 1e9))
    if SIZE and size != SIZE:
        print("注意：上游大小与脚本里记的 %d 不一致，以上游为准" % SIZE)
    download(size)

    with open(OUT, "wb") as fo:
        for i in range(N):
            part = os.path.join(OUT_DIR, "torch.part%d" % i)
            with open(part, "rb") as fi:
                while True:
                    b = fi.read(4 * 1024 * 1024)
                    if not b:
                        break
                    fo.write(b)
            os.remove(part)

    real = os.path.getsize(OUT)
    print("saved", OUT, real)
    if real != size:
        raise SystemExit("大小不符：%d != %d，wheel 已损坏，请重跑" % (real, size))
    print("大小校验通过。建议安装前再核对官方 sha256。")


if __name__ == "__main__":
    sys.exit(main())
