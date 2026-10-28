# -*- coding: utf-8 -*-
"""
tool_patch_spec.py   往 PyInstaller 生成的 spec 里注入"剔除 torch"的过滤

为什么需要这一步
----------------
`--exclude-module torch` 只作用于**模块图**，管不了 hook 收集来的二进制文件。
实测结果：torch 的 Python 文件被排除了、几百 MB 的 DLL 却照样进包，
打出一个 2.2 GB 的 EXE，而且 `import torch` 进来是
`module 'torch' has no attribute '__version__'` —— 又大又不能用。

所以在 Analysis 之后直接过滤 `a.binaries / a.datas / a.pure`，两头掐干净。
轻量版实测：2.2 GB -> 133 MB，torch 相关条目 0 个。

用法（build_exe.bat 里自动调）：
    python tool_patch_spec.py ACX720VoiceChanger.spec
"""

import io
import os
import sys

MARKER = "# >>> BEGIN DROP-TORCH FILTER"
ANCHOR = "pyz = PYZ("

FILTER = '''# >>> BEGIN DROP-TORCH FILTER （由 tool_patch_spec.py 注入，勿手改）>>>
# 轻量版不带 AI 引擎：`excludes` 只管模块图，管不了 hook 收集来的 DLL，
# 所以这里在 Analysis 之后把 torch / onnxruntime 相关的二进制、数据、
# 模块一次性过滤掉。需要 AI 分离请用 dist_ai 那个完整版。
_DROP_KEYS = (
    'torch', 'onnxruntime', 'triton', 'nvidia',
    'cudnn', 'cublas', 'cufft', 'cusparse', 'cusolver', 'curand',
    'nvrtc', 'nvjitlink', 'nvperf',
)


def _keep(item):
    """item 是 (name, path, typecode)；name 可能是 'torch\\\\lib\\\\x.dll'
    （binaries/datas）或 'torch' 这样的模块名（pure）。"""
    nm = str(item[0]).replace('\\\\', '/').lower()
    return not any(k in nm for k in _DROP_KEYS)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]
a.pure = [p for p in a.pure if _keep(p)]
# <<< END DROP-TORCH FILTER <<<
'''


def main():
    if len(sys.argv) < 2:
        print("用法：python tool_patch_spec.py <spec 文件>")
        return 1
    path = sys.argv[1]
    if not os.path.exists(path):
        print("找不到 %s" % path)
        return 1

    src = io.open(path, "r", encoding="utf-8").read()
    if MARKER in src:
        print("%s 已经注入过过滤，跳过" % os.path.basename(path))
        return 0
    if ANCHOR not in src:
        print("%s 里找不到锚点 '%s'，未改动" % (os.path.basename(path), ANCHOR))
        return 1

    out = src.replace(ANCHOR, FILTER + "\n" + ANCHOR, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(out)
    print("已注入过滤：%s" % os.path.basename(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
