#!/usr/bin/env bash
# 重新编译固件（需要 arm-none-eabi-gcc 在 PATH，或改下面的 GCC 变量）
# 本机工具链：C:/Users/s3xy/.workbuddy/tools/armgcc/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin
set -e
GCC="${GCC:-arm-none-eabi-gcc}"
OBJCOPY="${OBJCOPY:-arm-none-eabi-objcopy}"

"$GCC" -mcpu=cortex-m3 -mthumb -O2 -ffreestanding -fno-builtin \
  -nostdlib -nostartfiles -Wall -Wextra \
  -T stm32f103c8.ld startup.c main.c oled.c cfg_proto.c -o firmware.elf -Wl,-Map=firmware.map

"$OBJCOPY" -O ihex   firmware.elf firmware.hex
"$OBJCOPY" -O binary firmware.elf firmware.bin
echo "OK: firmware.hex / firmware.bin"
