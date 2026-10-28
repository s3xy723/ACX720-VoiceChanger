/* ============================================================================
 *  startup.c —— STM32F103C8T6 最小启动文件（C 写成，无汇编、无库依赖）
 *
 *  提供：向量表（含本固件用到的 EXTI4 / TIM2 / SPI1 / SysTick）、
 *        Reset_Handler（拷 .data、清 .bss、进 main）。
 *  链接脚本：stm32f103c8.ld
 * ==========================================================================*/
#include <stdint.h>

typedef void (*handler_t)(void);

extern uint32_t _estack;   /* 链接脚本给出：RAM 顶 */
extern uint32_t _sidata;   /* .data 在 Flash 中的装载地址 */
extern uint32_t _sdata;    /* .data 起始 */
extern uint32_t _edata;    /* .data 结束 */
extern uint32_t _sbss;     /* .bss 起始 */
extern uint32_t _ebss;     /* .bss 结束 */

int  main(void);
void Reset_Handler(void);
void Default_Handler(void);
void EXTI4_IRQHandler(void);
void TIM2_IRQHandler(void);
void SPI1_IRQHandler(void);
void SysTick_Handler(void);

__attribute__((section(".isr_vector"), used))
const handler_t g_vectors[52] = {
    /*  0 */ (handler_t)&_estack,      /* 初始 MSP */
    /*  1 */ Reset_Handler,
    /*  2 */ Default_Handler,          /* NMI */
    /*  3 */ Default_Handler,          /* HardFault */
    /*  4 */ Default_Handler,          /* MemManage */
    /*  5 */ Default_Handler,          /* BusFault */
    /*  6 */ Default_Handler,          /* UsageFault */
    /*  7 */ 0, 0, 0, 0,               /* 保留 */
    /* 11 */ Default_Handler,          /* SVC */
    /* 12 */ Default_Handler,          /* DebugMon */
    /* 13 */ 0,                        /* 保留 */
    /* 14 */ Default_Handler,          /* PendSV */
    /* 15 */ SysTick_Handler,
    /* 16 */ Default_Handler,          /* IRQ0  WWDG */
    /* 17 */ Default_Handler,          /* IRQ1  PVD */
    /* 18 */ Default_Handler,          /* IRQ2  TAMPER */
    /* 19 */ Default_Handler,          /* IRQ3  RTC */
    /* 20 */ Default_Handler,          /* IRQ4  FLASH */
    /* 21 */ Default_Handler,          /* IRQ5  RCC */
    /* 22 */ Default_Handler,          /* IRQ6  EXTI0 */
    /* 23 */ Default_Handler,          /* IRQ7  EXTI1 */
    /* 24 */ Default_Handler,          /* IRQ8  EXTI2 */
    /* 25 */ Default_Handler,          /* IRQ9  EXTI3 */
    /* 26 */ EXTI4_IRQHandler,         /* IRQ10 EXTI4   <-- PA4 = FPGA CS̄ 帧同步 */
    /* 27 */ Default_Handler,          /* IRQ11 DMA1_Ch1 */
    /* 28 */ Default_Handler,          /* IRQ12 DMA1_Ch2 */
    /* 29 */ Default_Handler,          /* IRQ13 DMA1_Ch3 */
    /* 30 */ Default_Handler,          /* IRQ14 DMA1_Ch4 */
    /* 31 */ Default_Handler,          /* IRQ15 DMA1_Ch5 */
    /* 32 */ Default_Handler,          /* IRQ16 DMA1_Ch6 */
    /* 33 */ Default_Handler,          /* IRQ17 DMA1_Ch7 */
    /* 34 */ Default_Handler,          /* IRQ18 ADC1_2 */
    /* 35 */ Default_Handler,          /* IRQ19 USB_HP */
    /* 36 */ Default_Handler,          /* IRQ20 USB_LP */
    /* 37 */ Default_Handler,          /* IRQ21 CAN_RX */
    /* 38 */ Default_Handler,          /* IRQ22 CAN_TX */
    /* 39 */ Default_Handler,          /* IRQ23 USB_WAKEUP */
    /* 40 */ Default_Handler,          /* IRQ24 TIM1_BRK */
    /* 41 */ Default_Handler,          /* IRQ25 TIM1_UP */
    /* 42 */ Default_Handler,          /* IRQ26 TIM1_TRG_COM */
    /* 43 */ Default_Handler,          /* IRQ27 TIM1_CC */
    /* 44 */ TIM2_IRQHandler,          /* IRQ28 TIM2    <-- 自检音 24.42 kHz */
    /* 45 */ Default_Handler,          /* IRQ29 TIM3 */
    /* 46 */ Default_Handler,          /* IRQ30 TIM4 */
    /* 47 */ Default_Handler,          /* IRQ31 I2C1_EV */
    /* 48 */ Default_Handler,          /* IRQ32 I2C1_ER */
    /* 49 */ Default_Handler,          /* IRQ33 I2C2_EV */
    /* 50 */ Default_Handler,          /* IRQ34 I2C2_ER */
    /* 51 */ SPI1_IRQHandler,          /* IRQ35 SPI1    <-- FPGA 样本流 */
};

__attribute__((noreturn))
void Reset_Handler(void)
{
    uint32_t *src = &_sidata;
    uint32_t *dst = &_sdata;
    while (dst < &_edata) { *dst++ = *src++; }        /* 拷 .data */
    dst = &_sbss;
    while (dst < &_ebss)  { *dst++ = 0u; }            /* 清 .bss  */

    (void)main();
    for (;;) { /* 不可达 */ }
}

void Default_Handler(void)
{
    for (;;) { /* 意外中断：停在这里，接调试器看 IPSR 定位 */ }
}
