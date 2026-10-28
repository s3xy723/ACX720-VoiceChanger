@echo off
rem  ------------------------------------------------------------------
rem  ACX720 voice changer host app - PyInstaller build script
rem
rem  Usage:
rem    build_exe.bat        lightweight onedir build only (default)
rem    build_exe.bat ai     + AI full build (torch/demucs/CUDA, ~4.8 GB, slow)
rem
rem  Output:
rem    ..\dist\ACX720VoiceChanger\ACX720VoiceChanger.exe
rem    ..\dist_ai\ACX720VoiceChangerAI\            (only with the "ai" arg)
rem
rem  [!] Encoding: this file is kept PURE ASCII with CRLF and no BOM.
rem      cmd.exe parses batch files byte-by-byte using the ANSI codepage, so
rem      UTF-8 Chinese silently turns commands into garbage ("...is not a
rem      command"). All Chinese notes live in docs and .py files instead.
rem
rem  [!] Always feed the .spec file to PyInstaller. Never use command-line
rem      --exclude-module: that regenerates an UNFILTERED spec and drags in
rem      hundreds of MB of torch DLLs (a 2.2 GB binary that fails at import
rem      with "module 'torch' has no attribute '__version__'"). The
rem      DROP-TORCH filter injected into ACX720VoiceChanger.spec by
rem      tool_patch_spec.py is what keeps it around 130 MB.
rem      To regenerate:  pyinstaller host_app.py --exclude-module ...
rem                      python tool_patch_spec.py ACX720VoiceChanger.spec
rem  ------------------------------------------------------------------

set PY=C:/Users/s3xy/.workbuddy/binaries/python/envs/py314/Scripts/python.exe
if not exist "%PY%" set PY=C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe
if not exist "%PY%" set PY=python
if /I not "%PY%"=="python" if not exist "%PY%" (
    echo Python for packaging not found: %PY%
    echo Edit the PY variable in this file.
    pause
    exit /b 1
)

cd /d "%~dp0"

echo [1/2] building icon (overwrites if present)
"%PY%" tool_make_icon.py

echo [2/2] PyInstaller onedir build, using the filtered spec
"%PY%" -m PyInstaller --noconfirm --distpath ..\dist --workpath ..\build_onedir ACX720VoiceChanger.spec
if errorlevel 1 (
    echo.
    echo Lightweight build FAILED - see the errors above.
    pause
    exit /b 1
)

echo.
echo Done: %~dp0..\dist\ACX720VoiceChanger\ACX720VoiceChanger.exe
dir "..\dist\ACX720VoiceChanger\ACX720VoiceChanger.exe"

if /I "%~1"=="ai" (
    echo.
    echo [AI] building the AI full version (torch/demucs/CUDA)...
    "%PY%" -m PyInstaller --noconfirm --distpath ..\dist_ai --workpath ..\build_ai ACX720VoiceChangerAI.spec
    if errorlevel 1 (
        echo.
        echo AI build FAILED - see the errors above.
        pause
        exit /b 1
    )
    echo AI done: %~dp0..\dist_ai\ACX720VoiceChangerAI\
)

echo.
echo Verify the bundled deps with the self-test:
echo   ..\dist\ACX720VoiceChanger\ACX720VoiceChanger.exe --selftest
if not "%~1"=="" pause
