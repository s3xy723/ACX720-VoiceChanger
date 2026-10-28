@echo off
rem  ACX720 FPGA bitstream programmer (Vivado Hardware Manager).
rem  Encoding note: pure ASCII + CRLF on purpose. cmd.exe reads batch files
rem  byte-by-byte with the ANSI codepage, so non-ASCII text or a BOM breaks
rem  whenever the system codepage is not 936. Chinese messages live in the
rem  Python scripts, which print Unicode straight to the console.
rem
rem  Usage:   double-click this file
rem           add --force to skip the "bitstream older than RTL" check
setlocal
cd /d "%~dp0"

set "PY=C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"
set "BIT=%~dp0..\vivado_prj\voice_changer.bit"

set "VIVADO="
for %%V in (
    "D:/AMDVivado/Vivado/2023.2/bin/vivado.bat"
    "D:/AMDVivado/2026.1/Vivado/bin/vivado.bat"
    "D:/Vivado/Vivado/2018.3/bin/vivado.bat"
    "D:/Xilinx/Vivado/2018.3/bin/vivado.bat"
    "C:/Xilinx/Vivado/2018.3/bin/vivado.bat"
    "C:/AMDDesignTools/Vivado/bin/vivado.bat"
) do (
    if not defined VIVADO if exist "%%~V" set "VIVADO=%%~V"
)
if not defined VIVADO (
    for /f "delims=" %%V in ('where vivado.bat 2^>nul') do (
        if not defined VIVADO set "VIVADO=%%V"
    )
)
if not defined VIVADO (
    echo.
    echo Vivado not found. Install Vivado / Vivado Lab, or edit this file.
    echo The CH340 on COM is a serial port, it cannot configure the FPGA.
    echo.
    pause
    exit /b 1
)
if not exist "%BIT%" (
    echo.
    echo Bitstream not found: %BIT%
    echo Rebuild it first:  rebuild_FPGA.bat
    echo.
    pause
    exit /b 1
)

rem ---- 2026-10-23: refuse to flash a bitstream older than the RTL ----
rem  A stale .bit gives a link that "works" (CRC 100%, counters moving) but a
rem  completely garbled speaker output. Checking timestamps first is cheap.
if /i "%~1"=="--force" goto flash
if not exist "%PY%" goto flash
"%PY%" "%~dp0check_bitstream_fresh.py"
if errorlevel 1 (
    echo.
    echo ABORT: the bitstream is older than the RTL sources.
    echo Flashing it now gives a working link but garbled audio.
    echo Rebuild first:  rebuild_FPGA.bat
    echo Force anyway:   add --force
    echo.
    pause
    exit /b 1
)

:flash
echo Using Vivado: %VIVADO%
echo Bitstream:     %BIT%
"%VIVADO%" -mode batch -source "%~dp0program.tcl"
if errorlevel 1 (
    echo.
    echo Programming failed. Check:
    echo   1. JTAG cable connected, board powered
    echo   2. Device Manager shows a Digilent/Xilinx cable
    echo   3. Vivado Hardware Manager can see xc7a35t
    echo.
)
pause
endlocal
