@echo off
rem  ACX720 FPGA audio bridge launcher
rem  Encoding note: this file is kept pure ASCII on purpose. cmd.exe reads
rem  batch files byte-by-byte with the ANSI codepage, so a UTF-8 BOM turns
rem  line 1 into garbage and non-ASCII text breaks whenever the system
rem  codepage isn't 936. All Chinese messages are printed by the Python
rem  script instead (it writes Unicode straight to the console).
setlocal
cd /d "%~dp0"
set "PY=C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"
if not exist "%PY%" (
    echo Python not found: %PY%
    echo Edit the PY variable in this file to point at an interpreter that
    echo has pyserial / numpy / sounddevice installed.
    pause
    exit /b 1
)
"%PY%" -u "%~dp0fpga_audio_bridge.py"
echo.
echo Bridge exited.
pause
endlocal
