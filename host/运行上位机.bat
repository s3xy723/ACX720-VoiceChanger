@echo off
chcp 65001 >nul
rem  ACX720 变声器上位机 启动脚本（源码模式）
rem
rem  按顺序找一个「同时有 numpy + tkinter + sounddevice」的 Python。
rem  旧的固定 venv 路径只在原来那台机器上存在；这里保留它作为首选，
rem  找不到就自动试本机 Python 3.15 / py 启动器 / PATH 里的 python。
rem
rem  不想装 Python 的话，也可以用打包好的 EXE（需自己重新打包，见 build_exe.bat）。

setlocal
cd /d "%~dp0"
set "PY="

rem 1) 原开发机的 venv（存在就用，且必须依赖齐全）
for %%P in ("C:\Users\s3xy\.workbuddy\binaries\python\envs\py314\Scripts\python.exe") do (
    if not defined PY if exist "%%~P" (
        "%%~P" -c "import numpy, tkinter, sounddevice" >nul 2>&1 && set "PY=%%~P"
    )
)

rem 2) 本机 Python（含 DeepSeek Harness 自带解释器：这台打包机在用它）
for %%P in ("%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe" "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe") do (
    if not defined PY if exist "%%~P" (
        "%%~P" -c "import numpy, tkinter, sounddevice" >nul 2>&1 && set "PY=%%~P"
    )
)

rem 3) py 启动器
if not defined PY (
    py -3 -c "import numpy, tkinter, sounddevice" >nul 2>&1 && set "PY=py"
)

rem 4) PATH 里的 python
if not defined PY (
    python -c "import numpy, tkinter, sounddevice" >nul 2>&1 && set "PY=python"
)

rem 5) 退而求其次：只要 numpy + tkinter，界面能开（声卡功能会置灰并说明原因）
if not defined PY (
    py -3 -c "import numpy, tkinter" >nul 2>&1 && set "PY=py"
)
if not defined PY (
    python -c "import numpy, tkinter" >nul 2>&1 && set "PY=python"
)
if not defined PY set "PY=python"

echo 使用 Python：%PY%
"%PY%" host_app.py
if errorlevel 1 (
    echo.
    echo 运行失败。常见原因：
    echo   1. 缺 numpy / sounddevice / soundfile —— 装一下：
    echo      %PY% -m pip install -r requirements.txt
    echo   2. Python 没带 tkinter —— 换一个完整安装的 Python 再试
    echo   3. 想用现成 EXE：..\dist\ACX720VoiceChanger\ACX720VoiceChanger.exe（文件夹版）
    pause
)
endlocal
