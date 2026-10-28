@echo off
chcp 65001 >nul
rem  烧录 STM32F103C8T6（ST-Link + OpenOCD）
rem  固件优先级：拖到本 bat 上的文件 > 新编译的 mcu\build\spi2_dma_dac.hex
rem              > 老的 mcu\f103_spi_mcp4922\firmware.hex
rem  前置：ST-Link 驱动已安装（设备管理器里 STM32 STLink 无感叹号）

setlocal
cd /d "%~dp0"
set "OOCD=%~dp0thirdparty\openocd\bin\openocd.exe"

set "HEX=%~dp0..\mcu\f103_spi_mcp4922\firmware.hex"
rem  2026-10-22 修正：当前 bench 硬件是 Design A（外置 MCP4922，SPI1 收 FPGA / SPI2 发 MCP4922）。
rem  mcu\build\spi2_dma_dac.hex 是 Design B（STM32 内部 DAC）旧设计，与当前接线不符，
rem  不再默认烧它。若确实要烧 Design B，请显式传参：
rem    烧录STM32.bat ..\mcu\build\spi2_dma_dac.hex
if not "%~1"=="" set "HEX=%~1"
set "HEX=%HEX:\=/%"

if not exist "%OOCD%" (
    echo 找不到 OpenOCD：%OOCD%
    echo 需要保留 tool\thirdparty\openocd\ 整个文件夹。
    pause
    exit /b 1
)
if not exist "%HEX%" (
    echo 找不到固件：%HEX%
    pause
    exit /b 1
)

echo 使用 OpenOCD：%OOCD%
echo 固件：%HEX%
"%OOCD%" -f interface/stlink.cfg -f target/stm32f1x.cfg ^
    -c "program {%HEX%} verify reset exit"
if errorlevel 1 (
    echo.
    echo 烧录失败。检查：
    echo   1. ST-Link 是否接好（SWDIO/SWCLK/GND/3.3V）
    echo   2. 设备管理器里 STM32 STLink 是否正常
    echo   3. 板子是否上电（OpenOCD 会显示 Target voltage）
    echo.
)
pause
endlocal
