/* ============================================================================
 *  f103_startup.c —— STM32F103C8T6 最小启动文件（配合 spi2_dma_dac.c）
 *
 *  为什么需要它：spi2_dma_dac.c 只是一个"裸算法"，芯片上电后并不会自己
 *  跑起来。Cortex-M3 要求 Flash 最开头是向量表（第 0 项 = 初始栈顶、
 *  第 1 项 = 复位入口），而 .data 要从 Flash 搬到 RAM、.bss 要清零，
 *  否则 g_dma_buf / g_left 这些全局量的初值全是随机的。
 *
 *  用法：arm-none-eabi-gcc f103_startup.c spi2_dma_dac.c -T f103.ld ...
 * ==========================================================================*/

typedef void (*isr_t)(void);

extern unsigned long __StackTop;
extern unsigned long _sidata;
extern unsigned long _sdata;
extern unsigned long _edata;
extern unsigned long _sbss;
extern unsigned long _ebss;

int main(void);

/* ---- 真正用到的三个中断，定义在 spi2_dma_dac.c ---- */
void DMA1_Channel4_IRQHandler(void);    /* IRQn 14 -> 向量表第 30 项 */
void EXTI15_10_IRQHandler(void);        /* IRQn 40 -> 向量表第 56 项 */
void TIM6_IRQHandler(void);             /* IRQn 54 -> 向量表第 70 项 */

/* ---- 兜底：进了没处理的中断就死循环，调试器一眼能看出停在哪 ---- */
static void Default_Handler(void)
{
    for (;;) {
    }
}

void Reset_Handler(void)
{
    unsigned long *src;
    unsigned long *dst;

    /* .data：把初值从 Flash 搬到 RAM */
    src = &_sidata;
    dst = &_sdata;
    while (dst < &_edata) {
        *dst++ = *src++;
    }

    /* .bss：清零 */
    dst = &_sbss;
    while (dst < &_ebss) {
        *dst++ = 0;
    }

    main();

    /* main 不应返回；真返回了就停在这，别乱跑 */
    for (;;) {
    }
}

/* ---- 向量表。第 N 项对应 IRQn = N - 16，顺序严格按 RM0008 ---- */
__attribute__((section(".isr_vector"), used))
const isr_t g_vector_table[] = {
    (isr_t)(&__StackTop),           /*  0 初始栈顶              */
    Reset_Handler,                  /*  1 复位                  */
    Default_Handler,                /*  2 NMI                   */
    Default_Handler,                /*  3 HardFault             */
    Default_Handler,                /*  4 MemManage             */
    Default_Handler,                /*  5 BusFault              */
    Default_Handler,                /*  6 UsageFault            */
    0, 0, 0, 0,                     /*  7..10 保留              */
    Default_Handler,                /* 11 SVC                   */
    Default_Handler,                /* 12 DebugMonitor          */
    0,                              /* 13 保留                  */
    Default_Handler,                /* 14 PendSV                */
    Default_Handler,                /* 15 SysTick               */
    Default_Handler,                /* 16 WWDG          IRQn 0  */
    Default_Handler,                /* 17 PVD           IRQn 1  */
    Default_Handler,                /* 18 TAMPER        IRQn 2  */
    Default_Handler,                /* 19 RTC           IRQn 3  */
    Default_Handler,                /* 20 FLASH         IRQn 4  */
    Default_Handler,                /* 21 RCC           IRQn 5  */
    Default_Handler,                /* 22 EXTI0         IRQn 6  */
    Default_Handler,                /* 23 EXTI1         IRQn 7  */
    Default_Handler,                /* 24 EXTI2         IRQn 8  */
    Default_Handler,                /* 25 EXTI3         IRQn 9  */
    Default_Handler,                /* 26 EXTI4         IRQn 10 */
    Default_Handler,                /* 27 DMA1_Ch1      IRQn 11 */
    Default_Handler,                /* 28 DMA1_Ch2      IRQn 12 */
    Default_Handler,                /* 29 DMA1_Ch3      IRQn 13 */
    DMA1_Channel4_IRQHandler,       /* 30 DMA1_Ch4 ★SPI2 RX DMA */
    Default_Handler,                /* 31 DMA1_Ch5      IRQn 15 */
    Default_Handler,                /* 32 DMA1_Ch6      IRQn 16 */
    Default_Handler,                /* 33 DMA1_Ch7      IRQn 17 */
    Default_Handler,                /* 34 ADC1_2        IRQn 18 */
    Default_Handler,                /* 35 USB_HP_CAN_TX IRQn 19 */
    Default_Handler,                /* 36 USB_LP_CAN_RX IRQn 20 */
    Default_Handler,                /* 37 CAN1_RX1      IRQn 21 */
    Default_Handler,                /* 38 CAN1_SCE      IRQn 22 */
    Default_Handler,                /* 39 EXTI9_5       IRQn 23 */
    Default_Handler,                /* 40 TIM1_BRK      IRQn 24 */
    Default_Handler,                /* 41 TIM1_UP       IRQn 25 */
    Default_Handler,                /* 42 TIM1_TRG_COM  IRQn 26 */
    Default_Handler,                /* 43 TIM1_CC       IRQn 27 */
    Default_Handler,                /* 44 TIM2          IRQn 28 */
    Default_Handler,                /* 45 TIM3          IRQn 29 */
    Default_Handler,                /* 46 TIM4          IRQn 30 */
    Default_Handler,                /* 47 I2C1_EV       IRQn 31 */
    Default_Handler,                /* 48 I2C1_ER       IRQn 32 */
    Default_Handler,                /* 49 I2C2_EV       IRQn 33 */
    Default_Handler,                /* 50 I2C2_ER       IRQn 34 */
    Default_Handler,                /* 51 SPI1          IRQn 35 */
    Default_Handler,                /* 52 SPI2          IRQn 36 */
    Default_Handler,                /* 53 USART1        IRQn 37 */
    Default_Handler,                /* 54 USART2        IRQn 38 */
    Default_Handler,                /* 55 USART3        IRQn 39 */
    EXTI15_10_IRQHandler,           /* 56 EXTI15_10 ★CS̄ 帧同步 */
    Default_Handler,                /* 57 RTCAlarm      IRQn 41 */
    Default_Handler,                /* 58 USBWakeUp     IRQn 42 */
    0, 0, 0, 0, 0,                  /* 59..63 保留（F103 在此断开） */
    0, 0, 0, 0, 0, 0,               /* 64..69 保留               */
    TIM6_IRQHandler,                /* 70 TIM6 ★DAC 输出节拍     */
    Default_Handler,                /* 71 TIM7          IRQn 55  */
};
