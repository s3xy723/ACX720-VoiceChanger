@echo off
chcp 65001 >nul
rem  编译 STM32F103C8T6 音响固件（SPI2 DMA + DAC）
rem  源码：..\mcu\f103_startup.c + ..\mcu\spi2_dma_dac.c
rem  产物：..\mcu\build\spi2_dma_dac.elf / .hex / .bin
rem
rem  前置条件：装了 ARM 交叉编译器 arm-none-eabi-gcc（本机没装的话脚本会
rem  告诉你去哪儿下，不会静默失败）。

setlocal
cd /d "%~dp0"
set "SRC=%~dp0..\mcu"
set "OUT=%SRC%\build"

echo === 1/4 找编译器 ===
set "CC="
for %%P in (
    "arm-none-eabi-gcc.exe"
    "C:\Program Files (x86)\GNU Arm Embedded Toolchain\10 2021.10\bin\arm-none-eabi-gcc.exe"
    "C:\Program Files (x86)\GNU Tools ARM Embedded\9 2019-q4-major\bin\arm-none-eabi-gcc.exe"
    "C:\Program Files\GNU Arm Embedded Toolchain\10 2021.10\bin\arm-none-eabi-gcc.exe"
    rem  winget 装的 14.2.Rel1（默认落到下面这两个位置之一）
    "C:\Program Files\Arm GNU Toolchain arm-none-eabi\14.2 rel1\bin\arm-none-eabi-gcc.exe"
    "C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\14.2 rel1\bin\arm-none-eabi-gcc.exe"
    rem  2026-10-22 本机实际用的：zip 免安装版（装 winget 版要管理员权限，跑不了）
    "C:\Users\Administrator\tools\gcc-arm-none-eabi\bin\arm-none-eabi-gcc.exe"
    "C:\ST\STM32CubeIDE_1.13.0\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.10.3-2021.10.win32_1.0.0.202111181127\tools\bin\arm-none-eabi-gcc.exe"
) do (
    if not defined CC (
        where %%~P >nul 2>nul && set "CC=%%~P"
    )
)
if not defined CC (
    if exist "%~dp0..\..\..\gcc-arm-none-eabi\bin\arm-none-eabi-gcc.exe" (
        set "CC=%~dp0..\..\..\gcc-arm-none-eabi\bin\arm-none-eabi-gcc.exe"
    )
)

if not defined CC (
    echo.
    echo [未找到] 本机没有 arm-none-eabi-gcc，无法编译。
    echo.
    echo 三个办法任选其一：
    echo   1. 装 ARM GNU Toolchain（推荐，免费）
    echo      https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads
    echo      选 Windows 的 arm-none-eabi 版本，装完把 bin 目录加进 PATH。
    echo   2. 装 STM32CubeIDE，用它自带的编译器
    echo      https://www.st.com/en/development-tools/stm32cubeide.html
    echo      装好后把上面那条 C:\ST\... 路径按实际版本改一下。
    echo   3. 用 Keil / IAR 建工程，把 mcu\spi2_dma_dac.c 和
    echo      mcu\f103_startup.c 加进去，链接脚本换成厂家的 scatter/icf 即可。
    echo.
    echo 逻辑正确性不依赖编译器，可以先跑：
    echo      python tool\verify_mcu_dac_driver.py
    echo （把驱动的算法移植到 Python 逐样本验证，不需要硬件也不需要编译器。）
    echo.
    pause
    exit /b 1
)
echo 编译器：%CC%

echo.
echo === 2/4 建输出目录 ===
if not exist "%OUT%" mkdir "%OUT%"

echo.
echo === 3/4 编译链接 ===
"%CC%" -mcpu=cortex-m3 -mthumb -O2 -g ^
    -ffunction-sections -fdata-sections ^
    -Wall -Wextra -Wno-unused-parameter ^
    -ffreestanding -nostdlib ^
    -T "%SRC%\f103.ld" ^
    -o "%OUT%\spi2_dma_dac.elf" ^
    "%SRC%\f103_startup.c" "%SRC%\spi2_dma_dac.c" ^
    -Wl,-Map="%OUT%\spi2_dma_dac.map" -Wl,--gc-sections -Wl,--print-memory-usage
if errorlevel 1 (
    echo.
    echo 编译失败。常见原因：
    echo   - 链接脚本路径不对（确认 mcu\f103.ld 存在）
    echo   - 提示 "region FLASH overflowed"：代码太大，C8T6 只有 64 KB
    echo.
    pause
    exit /b 1
)

echo.
echo === 4/4 生成 hex / bin ===
for %%F in ("%CC%") do set "BIN=%%~dpF"
"%BIN%arm-none-eabi-objcopy.exe" -O ihex "%OUT%\spi2_dma_dac.elf" "%OUT%\spi2_dma_dac.hex"
"%BIN%arm-none-eabi-objcopy.exe" -O binary "%OUT%\spi2_dma_dac.elf" "%OUT%\spi2_dma_dac.bin"
"%BIN%arm-none-eabi-size.exe" "%OUT%\spi2_dma_dac.elf"

echo.
echo 完成：
echo   %OUT%\spi2_dma_dac.hex   （OpenOCD / STM32CubeProgrammer 烧录用）
echo   %OUT%\spi2_dma_dac.bin
echo.
echo 下一步：改 烧录STM32.bat 里的 HEX 路径指向上面的 .hex，然后烧录。
echo   OpenOCD 命令行等价写法：
echo     openocd -f interface/stlink.cfg -f target/stm32f1x.cfg ^
echo         -c "program {mcu/build/spi2_dma_dac.hex} verify reset exit"
echo.
pause
endlocal
