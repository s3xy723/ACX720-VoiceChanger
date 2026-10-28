@echo off
chcp 65001 >nul
rem  安装 ST-Link USB 驱动（stlink_dbg_winusb.inf）
rem  需要管理员权限；脚本会自己请求 UAC。

net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "INF=%~dp0thirdparty\stlink_driver\stlink_dbg_winusb.inf"
if not exist "%INF%" (
    echo 找不到驱动 INF：%INF%
    pause
    exit /b 1
)

echo 正在安装：%INF%
pnputil /add-driver "%INF%" /install
if errorlevel 1 (
    echo.
    echo 安装失败。若设备仍显示感叹号，拔插一次 ST-Link 再试。
) else (
    echo.
    echo 安装完成。设备管理器里应显示 "STM32 STLink" 且无感叹号。
)
pause
