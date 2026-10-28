/* ============================================================================
 *  f103.ld —— STM32F103C8T6 的最小链接脚本（配合 spi2_dma_dac.c 使用）
 *
 *  C8T6：Flash 64 KB @ 0x08000000，SRAM 20 KB @ 0x20000000。
 *  栈放在 RAM 顶端向下生长；heap 留 0（本工程不用 malloc）。
 * ==========================================================================*/

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 20K
}

/* 栈顶 = RAM 末尾。留 1 KB 给栈，够用了（中断不嵌套）。 */
__StackTop = ORIGIN(RAM) + LENGTH(RAM);
__StackLimit = __StackTop - 1K;

SECTIONS
{
    /* 向量表必须在 Flash 的最开头 */
    .isr_vector :
    {
        . = ALIGN(4);
        KEEP(*(.isr_vector))
        . = ALIGN(4);
    } > FLASH

    .text :
    {
        . = ALIGN(4);
        *(.text)
        *(.text*)
        *(.rodata)
        *(.rodata*)
        *(.glue_7)
        *(.glue_7t)
        . = ALIGN(4);
        _etext = .;
    } > FLASH

    .ARM.exidx :
    {
        *(.ARM.exidx*)
    } > FLASH

    /* 有初值的全局量：Flash 里放初值，启动时搬到 RAM */
    .data : AT (_etext)
    {
        . = ALIGN(4);
        _sdata = .;
        *(.data)
        *(.data*)
        . = ALIGN(4);
        _edata = .;
    } > RAM
    _sidata = LOADADDR(.data);

    /* 无初值的全局量：启动时清零 */
    .bss :
    {
        . = ALIGN(4);
        _sbss = .;
        *(.bss)
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        _ebss = .;
    } > RAM

    ._user_heap_stack :
    {
        . = ALIGN(8);
        . = . + 0x400;          /* 1 KB 栈空间占位，防止溢出到 .bss */
        . = ALIGN(8);
    } > RAM

    /DISCARD/ :
    {
        *(.note.GNU-stack)
    }
}

ASSERT(__StackLimit >= _ebss, "RAM 不够：.bss + 栈 超出了 20 KB")
