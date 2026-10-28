@echo off
rem  Rebuild the ACX720 FPGA bitstream: synth -> impl -> write_bitstream,
rem  then copy the fresh .bit to ..\vivado_prj\voice_changer.bit.
rem  Encoding note: pure ASCII + CRLF on purpose (see the programmer bat).
rem  Takes 3-8 minutes. Run after ANY change under ..\rtl or ..\constr.
setlocal
cd /d "%~dp0"

set "PY=C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"
if not exist "%PY%" (
    echo Python not found: %PY%
    echo Edit the PY variable in this file.
    pause
    exit /b 1
)
"%PY%" -u "%~dp0rebuild_bitstream.py"
echo.
pause
endlocal
