@echo off
rem ---------------------------------------------------------------
rem  Double-click this file to create the GitHub repo and push.
rem  It only calls tool\push_to_github.py -- all the real logic
rem  (and every message you see) lives in that Python script.
rem
rem  NOTE: keep this file pure ASCII + CRLF + no BOM. cmd.exe reads
rem  .bat byte by byte in the OEM code page, so non-ASCII content
rem  gets garbled and can even trigger the wrong command.
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   ACX720 VoiceChanger  --  push to GitHub
echo ============================================================
echo.

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.8+ or run tool\push_to_github.py manually.
    echo.
    pause
    exit /b 1
)

"%PY%" "tool\push_to_github.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Exit code: %RC%
pause
exit /b %RC%
