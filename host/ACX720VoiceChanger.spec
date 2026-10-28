# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
for _m in ('sounddevice', 'soundfile', 'av', 'numpy'):
    tmp_ret = collect_all(_m)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]

# 外部 torch 会 import 主程序没用到的标准库（实测第一个漏的是 timeit）。
# 只补实际缺的，避免把 pandas/PIL 等整条依赖链拖进来。
hiddenimports += ['timeit', 'pickletools']

# 这几个是 host_app 里**函数内部** import 的模块：防啸叫只在文件处理时用、
# 年龄参数包只在点"发送到 FPGA"时用、age_model 又是 age_link 的依赖。
# 静态分析有时能把它们收进来，但漏了不会报错、只会让功能静默失效，
# 所以显式列出来（打包后用 --selftest 会真的 import 一遍验证）。
hiddenimports += ['anti_howl', 'age_link', 'age_model']


a = Analysis(
    ['host_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['demucs', 'torch', 'torchaudio', 'yt_dlp', 'websockets',
              'requests', 'urllib3', 'curl_cffi', 'certifi', 'brotli',
              'secretstorage', 'mutagen', 'Cryptodome',
              # 这些是 local_ai 里 huggingface_hub 的可选依赖，运行时从
              # 用户级 site-packages 拿即可；打进来会白增 ~40 MB。
              'huggingface_hub', 'hf_xet', 'pandas', 'PIL', 'lxml',
              'safetensors', 'tokenizers', 'transformers'],
    noarchive=False,
    optimize=0,
)

# 轻量版：把 torch / onnxruntime / CUDA 相关 DLL 彻底剔掉。
# 只写 excludes= 管不住 hook 收集来的二进制，必须在 Analysis 之后过滤。
_DROP_KEYS = (
    'torch', 'onnxruntime', 'triton', 'nvidia',
    'cudnn', 'cublas', 'cufft', 'cusparse', 'cusolver', 'curand',
    'nvrtc', 'nvjitlink', 'nvperf',
)


def _keep(item):
    nm = str(item[0]).replace('\\', '/').lower()
    return not any(k in nm for k in _DROP_KEYS)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]
a.pure = [p for p in a.pure if _keep(p)]

pyz = PYZ(a.pure)

# ★ 用 onedir（文件夹版），不用 onefile。
#   单文件版每次启动都要把几十 MB 解压到 %TEMP%\_MEIxxxx；某些机器上
#   （杀软/权限/临时目录策略）会直接弹 "Could not create temporary
#   directory!"，app 根本起不来。文件夹版不写临时目录，双击即开。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ACX720VoiceChanger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ACX720VoiceChanger',
)
