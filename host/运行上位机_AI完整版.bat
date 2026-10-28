@echo off
chcp 65001 >nul
rem  ==========================================================================
rem   ACX720 变声器上位机 —— AI 完整版启动器
rem
rem   和「运行上位机.bat」的区别：那个只保证基础功能能跑；
rem   这个会在启动前自检 torch / demucs / CUDA，只有「分离 × 计算机本地模型」
rem   这个组合需要它们（demucs 神经网络分离，GPU 加速）。
rem   单文件 EXE（dist\ACX720VoiceChanger.exe）里刻意没有带 torch ——
rem   torch cu128 全套 4.2 GB，打进 EXE 会让程序变成 4 GB 且每次启动解压几分钟。
rem   要用完整 AI，就用这个脚本（跑源码，调本机已装好的 torch）。
rem  ==========================================================================

rem  venv 的解释器在 Scripts\ 下，不在 venv 根目录
set PY=C:\Users\s3xy\.workbuddy\binaries\python\envs\py314\Scripts\python.exe
if not exist "%PY%" set PY=python

cd /d "%~dp0"

echo 正在自检 AI 依赖（torch / demucs / CUDA）...
"%PY%" tool_check_ai.py
if errorlevel 1 (
    echo.
    echo ================= 依赖不完整，AI 分离不可用 =================
    echo.
    echo 请按上面的提示安装。常见装法：
    echo.
    echo   1. torch + torchaudio（CUDA 12.8 版，约 4 GB）
    echo      "%PY%" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
    echo.
    echo   2. demucs（约 1 MB，会自动带上 huggingface_hub 等）
    echo      "%PY%" -m pip install demucs
    echo.
    echo   3. 国内网络拉不动 huggingface.co 的权重时，本程序会自动改用
    echo      hf-mirror.com 镜像，不需要手动设置。
    echo.
    echo 只想用非 AI 功能的话，直接跑「运行上位机.bat」或 dist 里的单文件 EXE。
    echo.
    pause
    exit /b 1
)

echo.
echo 依赖齐全，启动上位机...
"%PY%" host_app.py
if errorlevel 1 (
    echo.
    echo 运行失败。请把上面的报错发出来。
    pause
)
