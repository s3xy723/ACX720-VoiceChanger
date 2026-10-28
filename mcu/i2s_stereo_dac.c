/* ============================================================================
 *  i2s_stereo_dac.c
 *  STM32F103C8T6 —— 从 FPGA 收 I2S 双声道，用片内双通道 DAC 还原成模拟立体声
 *
 *  链路：
 *      麦克风 -> ES8388(ADC) -> FPGA 变声处理
 *            -> GPIO0 扩展口三条线（mcu_bclk / mcu_lrck / mcu_dout）
 *            -> 本程序（I2S2 从机接收）-> DAC1(PA4) / DAC2(PA5)
 *            -> 两级 RC 低通 -> 隔直电容 -> PAM8403 功放 -> 左右音箱
 *
 *  为什么这么写
 *  ------------
 *  1) 纯寄存器级，不依赖 HAL / 标准库。Keil / IAR / GCC 都能直接编，
 *     也方便看清每一个位在干什么。
 *  2) 采样率由 FPGA 侧的 ES8388 决定（24.414 kHz = MCLK 12.5 MHz / 512），
 *     单片机是从机，只跟着 BCLK / LRCK 走，不产生任何时钟 —— 这样两边
 *     永远不会漂移，也就不需要异步 FIFO 或采样率转换。
 *  3) DAC 用"双通道同时装载"寄存器 DHR12RD，一次写 32 位同时更新两路，
 *     左右声道之间的相位差为零。
 *
 *  总线格式（与 rtl/i2s_slave.v、rtl/i2s_mirror_tx.v 完全一致）
 *  ------------------------------------------------------------
 *      BCLK  1.5625 MHz
 *      LRCK 24.414  kHz       每声道 32 个 BCLK（即 64 BCLK / 帧）
 *      数据  16 bit，MSB 先行，Philips 标准
 *      时隙  0 = 无效位；1..16 = bit15..bit0；17..31 = 补零
 *
 *  对应的 I2S 配置字（这是最容易配错的一处，两个独立信源核对过）
 *  ------------------------------------------------------------
 *      DATLEN[2:1] = 00   （16 位有效数据）
 *      CHLEN[0]    = 1    （32 位通道长度）
 *      两者合起来 = 标准库的 I2S_DataFormat_16bextended = 0x0001
 *      RM0008 的"关闭 I2S"步骤里写的也是 "DATLEN = 00 and CHLEN = 1"，
 *      与 stm32f10x_spi.h 的宏值一致。
 *
 *  关于 RXNE 的次数（RM0008 说得含糊，代码做了两手准备）
 *  ------------------------------------------------------------
 *  RM0008 原话："Depending on the data and channel length configuration,
 *  the audio value received for a right or left channel may result from
 *  one or two receptions into the Rx buffer."
 *  也就是说：16bit-extended 到底是"每声道 1 次 RXNE"还是"2 次（数据 + 补零）"
 *  手册没有给死。下面的 ISR 用 **CHSIDE 跳变门控** 处理：
 *      只有 CHSIDE 发生变化的那个半字才当作有效数据，
 *      同一个 CHSIDE 下再来的半字一律丢掉。
 *  这样无论硬件给 1 次还是 2 次 RXNE，解出来的样本都正确。
 *  串口会打印 dummy 计数，实测是哪种一眼就知道。
 *
 *  编译
 *  ----
 *      Keil MDK：新建 STM32F103C8 工程，把本文件加进去即可。
 *                 不要勾选 "Use MicroLIB" 之外的 CMSIS 冲突项。
 *      GCC     ：arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 \
 *                    -ffreestanding -T stm32f103c8.ld \
 *                    -o out.elf i2s_stereo_dac.c startup_stm32f10x_md.c
 *      向量表用工具链自带的启动文件（文件末尾有说明），这里不重复定义。
 * ==========================================================================*/

#include <stdint.h>

/* ------------------------------------------------------------------ 可调项 */

/* 数字音量：0 = 原样，1 = 半音量（-6 dB），2 = 四分之一。
   推无源音箱时如果 PAM8403 自带电位器，这里保持 0，用功放的旋钮调。 */
#ifndef VOL_SHIFT
#define VOL_SHIFT   0
#endif

/* 左右对调：觉得左右反了就改成 1（也可以直接换音箱接线） */
#ifndef SWAP_LR
#define SWAP_LR     0
#endif

/* 串口诊断：PA9(TX) / PA10(RX) 115200 8N1，每 500 ms 打一行状态。
   不要就把它改成 0，省 1 KB 左右。 */
#ifndef ENABLE_UART_LOG
#define ENABLE_UART_LOG  1
#endif

/* ------------------------------------------------------------------ 地址 */

#define PERIPH_BASE      0x40000000u
#define APB1PERIPH_BASE  (PERIPH_BASE + 0x00000u)
#define APB2PERIPH_BASE  (PERIPH_BASE + 0x10000u)
#define AHBPERIPH_BASE   (PERIPH_BASE + 0x20000u)

#define RCC_BASE         (AHBPERIPH_BASE  + 0x1000u)
#define AFIO_BASE        (APB2PERIPH_BASE + 0x0000u)
#define GPIOA_BASE       (APB2PERIPH_BASE + 0x0800u)
#define GPIOB_BASE       (APB2PERIPH_BASE + 0x0C00u)
#define GPIOC_BASE       (APB2PERIPH_BASE + 0x1000u)
#define USART1_BASE      (APB2PERIPH_BASE + 0x3800u)
#define SPI2_BASE        (APB1PERIPH_BASE + 0x3800u)
#define DAC_BASE         (APB1PERIPH_BASE + 0x7400u)

#define SCS_BASE         0xE000E000u
#define SYSTICK_BASE     (SCS_BASE + 0x0010u)
#define NVIC_BASE        (SCS_BASE + 0x0100u)

#define RCC    ((volatile uint32_t *)RCC_BASE)
#define GPIOA  ((volatile uint32_t *)GPIOA_BASE)
#define GPIOB  ((volatile uint32_t *)GPIOB_BASE)
#define GPIOC  ((volatile uint32_t *)GPIOC_BASE)
#define AFIO   ((volatile uint32_t *)AFIO_BASE)
#define USART1 ((volatile uint32_t *)USART1_BASE)
#define SPI2   ((volatile uint32_t *)SPI2_BASE)
#define DAC    ((volatile uint32_t *)DAC_BASE)
#define STK    ((volatile uint32_t *)SYSTICK_BASE)
#define NVIC   ((volatile uint32_t *)NVIC_BASE)

typedef volatile uint32_t reg32_t;

/* RCC */
#define RCC_CR          RCC[0x00 / 4]
#define RCC_CFGR        RCC[0x04 / 4]
#define RCC_APB2ENR     RCC[0x18 / 4]
#define RCC_APB1ENR     RCC[0x1C / 4]

#define RCC_CR_HSEON        (1u << 16)
#define RCC_CR_HSERDY       (1u << 17)
#define RCC_CR_PLLON        (1u << 24)
#define RCC_CR_PLLRDY       (1u << 25)

#define RCC_CFGR_PLLSRC     (1u << 16)      /* 1 = HSE 作为 PLL 输入 */
#define RCC_CFGR_PLLXTPRE   (1u << 17)      /* HSE 分频（此处不清零即不分频） */
#define RCC_CFGR_PLLMUL_SHIFT 18
#define RCC_CFGR_PPRE1_SHIFT 8
#define RCC_CFGR_SWS_PLL    (2u << 2)
#define RCC_CFGR_SW_PLL     (2u << 0)
#define RCC_CFGR_SWS_HSI    (0u << 2)

#define RCC_APB2ENR_AFIOEN  (1u << 0)
#define RCC_APB2ENR_IOPAEN  (1u << 2)
#define RCC_APB2ENR_IOPBEN  (1u << 3)
#define RCC_APB2ENR_IOPCEN  (1u << 4)
#define RCC_APB2ENR_USART1EN (1u << 14)

#define RCC_APB1ENR_SPI2EN  (1u << 14)
#define RCC_APB1ENR_DACEN   (1u << 29)

/* GPIO */
#define GPIO_CRL        0x00
#define GPIO_CRH        0x04
#define GPIO_IDR        0x08
#define GPIO_ODR        0x0C
#define GPIO_BSRR       0x10
#define GPIO_BRR        0x14

/* SPI2 / I2S2 */
#define SPI_CR1         0x00
#define SPI_CR2         0x04
#define SPI_SR          0x08
#define SPI_DR          0x0C
#define SPI_I2SCFGR     0x1C
#define SPI_I2SPR       0x20

#define SPI_CR1_SSM         (1u << 9)      /* 软件管理 NSS */
#define SPI_CR2_RXNEIE      (1u << 6)
#define SPI_CR2_ERRIE       (1u << 5)
#define SPI_SR_RXNE         (1u << 0)
#define SPI_SR_CHSIDE       (1u << 2)
#define SPI_SR_UDR          (1u << 3)
#define SPI_SR_OVR          (1u << 6)
#define SPI_SR_BSY          (1u << 7)

/* I2SCFGR 位 */
#define I2S_I2SMOD          (1u << 11)     /* 1 = I2S 模式（不是 SPI） */
#define I2S_I2SE            (1u << 10)     /* I2S 使能 */
#define I2S_I2SCFG_SLAVERX  (1u << 8)      /* I2SCFG = 01：从机接收 */
#define I2S_PCMSYNC         (1u << 7)      /* 0 = 短帧同步 */
#define I2S_I2SSTD_PHILIPS  (0u << 4)      /* I2SSTD = 00 */
#define I2S_CKPOL_LOW       (0u << 3)      /* CK 空闲为低 */
#define I2S_DATLEN_16       (0u << 1)      /* DATLEN = 00：16 位数据 */
#define I2S_CHLEN_32        (1u << 0)      /* CHLEN  = 1 ：32 位通道 */

/* DAC */
#define DAC_CR          0x00
#define DAC_SWTRIGR     0x04
#define DAC_DHR12RD     0x20               /* 双通道同时装载：低 12 位 = CH1(PA4)，[27:16] = CH2(PA5) */
#define DAC_DOR1        0x2C
#define DAC_DOR2        0x30

#define DAC_CR_EN1          (1u << 0)
#define DAC_CR_EN2          (1u << 16)
/* BOFF1/BOFF2 保持 0 = 输出缓冲打开。关掉能轨到轨但输出阻抗变高，
   带不动后面的 RC 滤波网络，所以这里必须开。 */
/* TEN1/TEN2 保持 0 = 不等待外部触发，写 DHR 后自动装载到 DOR。 */
#define DAC_CR_TEN1         (1u << 2)
#define DAC_CR_TEN2         (1u << 18)

/* USART1 */
#define USART_SR        0x00
#define USART_DR        0x04
#define USART_BRR       0x08
#define USART_CR1       0x0C
#define USART_CR2       0x10
#define USART_SR_TXE        (1u << 7)
#define USART_CR1_UE        (1u << 13)
#define USART_CR1_TE        (1u << 3)
#define USART_CR1_RE        (1u << 2)

/* SysTick */
#define STK_CTRL            STK[0]
#define STK_LOAD            STK[1]
#define STK_VAL             STK[2]
#define STK_CTRL_ENABLE     (1u << 0)
#define STK_CTRL_TICKINT    (1u << 1)
#define STK_CTRL_CLKSOURCE  (1u << 2)

/* NVIC：SPI2_IRQn = 36 -> ISER1 的第 4 位 */
#define NVIC_ISER0          NVIC[0]
#define NVIC_ISER1          NVIC[1]

/* ------------------------------------------------------------------ 全局 */

static volatile uint16_t g_left  = 0x8000u;   /* 静音电平，上电不炸音 */
static volatile uint16_t g_right = 0x8000u;
static volatile uint32_t g_frames = 0;        /* 收到的立体声帧数 */
static volatile uint32_t g_dummy = 0;         /* 被 CHSIDE 门控丢掉的补零半字 */
static volatile uint32_t g_ovr   = 0;         /* I2S 溢出次数（正常应为 0） */
static volatile int      g_side  = -1;        /* 上一次的 CHSIDE，-1 = 还没开始 */
static volatile uint32_t g_ms    = 0;         /* 毫秒计数（SysTick） */

/* -------------------------------------------------------------- 时钟初始化 */

/* 目标：SYSCLK 72 MHz / AHB 72 / APB2 72 / APB1 36 / USB 不用
 * 优先用板载 8 MHz 晶振（HSE）经 PLL ×9；
 * 蓝 pill 有些批次晶振不起振，检测不到就退到内部 HSI（8 MHz / 2 × 16 = 64 MHz）。
 * 两条路都够用：I2S 从机的内部分频只要在 2 倍以上位时钟即可，
 * 这里最低也是 36 MHz / 1.5625 MHz ≈ 23 倍。 */
static uint32_t clock_init(void)
{
    uint32_t sysclk;
    uint32_t timeout;

    /* --- 开 HSE --- */
    RCC_CR |= RCC_CR_HSEON;
    for (timeout = 0; timeout < 200000u; timeout++) {
        if (RCC_CR & RCC_CR_HSERDY)
            break;
    }

    if (RCC_CR & RCC_CR_HSERDY) {
        /* HSE 就绪：8 MHz / 1 × 9 = 72 MHz */
        RCC_CFGR &= ~(0xFu << RCC_CFGR_PLLMUL_SHIFT);
        RCC_CFGR |= ((9u - 2u) << RCC_CFGR_PLLMUL_SHIFT);
        RCC_CFGR |= RCC_CFGR_PLLSRC;              /* PLL 输入 = HSE，不分频 */
        RCC_CFGR &= ~RCC_CFGR_PLLXTPRE;
        sysclk = 72000000u;
    } else {
        /* 没有 HSE：HSI 8 MHz / 2 × 16 = 64 MHz（HSI 进 PLL 前自动二分频） */
        RCC_CR &= ~RCC_CR_HSEON;
        RCC_CFGR &= ~RCC_CFGR_PLLSRC;             /* PLL 输入 = HSI/2 */
        RCC_CFGR &= ~(0xFu << RCC_CFGR_PLLMUL_SHIFT);
        RCC_CFGR |= ((16u - 2u) << RCC_CFGR_PLLMUL_SHIFT);
        sysclk = 64000000u;
    }

    /* APB1 二分频（不超过 36 MHz），APB2 不分频 */
    RCC_CFGR &= ~(0x7u << RCC_CFGR_PPRE1_SHIFT);
    RCC_CFGR |=  (0x4u << RCC_CFGR_PPRE1_SHIFT);  /* PPRE1 = 100 -> /2 */

    RCC_CR |= RCC_CR_PLLON;
    for (timeout = 0; timeout < 200000u; timeout++) {
        if (RCC_CR & RCC_CR_PLLRDY)
            break;
    }
    RCC_CFGR &= ~0x3u;
    RCC_CFGR |= RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & (0x3u << 2)) != RCC_CFGR_SWS_PLL)
        ;

    /* Flash 预取：72 MHz 下必须开，否则取指会拖慢 */
    (*(volatile uint32_t *)0x40022000u) |= (1u << 4) | 0x2u;   /* FLASH_ACR: PRFTBE + LATENCY=2 */

    return sysclk;
}

/* -------------------------------------------------------------- GPIO 初始化 */

static void gpio_init(void)
{
    /* PB12 = I2S2_WS (LRCK)  -> 浮空输入
       PB13 = I2S2_CK (BCLK)  -> 浮空输入
       PB15 = I2S2_SD (数据)  -> 浮空输入
       注意这三个脚是"外设输入"，F1 上输入模式不需要 AFIO 重映射，
       也不需要把 MODE 设成复用 —— 复用功能输入直接配成输入即可。 */
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPBEN
                 | RCC_APB2ENR_IOPCEN | RCC_APB2ENR_AFIOEN;

    /* PB12/13/15 在 CRH 里：pin12 -> [19:16]，pin13 -> [23:20]，pin15 -> [31:28]
       CNF=01 MODE=00 -> 0x4 浮空输入 */
    GPIOB[GPIO_CRH / 4] &= ~((0xFu << 16) | (0xFu << 20) | (0xFu << 28));
    GPIOB[GPIO_CRH / 4] |=  ((0x4u << 16) | (0x4u << 20) | (0x4u << 28));

    /* PA4 / PA5 = DAC_OUT1 / DAC_OUT2 -> 模拟输入模式（CNF=00 MODE=00 = 0x0）
       pin4 -> CRL[19:16]，pin5 -> CRL[23:20] */
    GPIOA[GPIO_CRL / 4] &= ~((0xFu << 16) | (0xFu << 20));

    /* PC13 = 板载 LED（低电平点亮）-> 推挽输出 50 MHz */
    GPIOC[GPIO_CRH / 4] &= ~(0xFu << 20);
    GPIOC[GPIO_CRH / 4] |=  (0x3u << 20);
    GPIOC[GPIO_BSRR / 4] = (1u << 13);       /* 先灭 */

#if ENABLE_UART_LOG
    /* PA9 = USART1_TX -> 复用推挽 0xB；PA10 = USART1_RX -> 浮空输入 0x4 */
    GPIOA[GPIO_CRH / 4] &= ~((0xFu << 4) | (0xFu << 8));
    GPIOA[GPIO_CRH / 4] |=  ((0xBu << 4) | (0x4u << 8));
#endif
}

/* -------------------------------------------------------------- USART 诊断 */

#if ENABLE_UART_LOG
static void uart_init(uint32_t pclk2)
{
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    /* BRR = pclk2 / baud，四舍五入 */
    USART1[USART_BRR / 4] = (pclk2 + 57600u) / 115200u;
    USART1[USART_CR2 / 4] = 0;
    USART1[USART_CR1 / 4] = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
}

static void uart_putc(char c)
{
    while (!(USART1[USART_SR / 4] & USART_SR_TXE))
        ;
    USART1[USART_DR / 4] = (uint32_t)c;
}

static void uart_puts(const char *s)
{
    while (*s)
        uart_putc(*s++);
}

static void uart_put_hex16(uint16_t v)
{
    static const char *hex = "0123456789ABCDEF";
    int i;
    uart_putc('0'); uart_putc('x');
    for (i = 12; i >= 0; i -= 4)
        uart_putc(hex[(v >> i) & 0xF]);
}

static void uart_put_dec(uint32_t v)
{
    char buf[12];
    int i = 0;
    if (v == 0) {
        uart_putc('0');
        return;
    }
    while (v > 0) {
        buf[i++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (i > 0)
        uart_putc(buf[--i]);
}
#else
static void uart_init(uint32_t pclk2) { (void)pclk2; }
static void uart_puts(const char *s) { (void)s; }
static void uart_put_hex16(uint16_t v) { (void)v; }
static void uart_put_dec(uint32_t v) { (void)v; }
#endif

/* -------------------------------------------------------------- DAC 初始化 */

static void dac_init(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_DACEN;

    /* 只开使能。TEN=0（不等待触发，写 DHR 后自动更新）、BOFF=0（缓冲打开）。
       两个通道一起使能，这样后面一次写 DHR12RD 就能同时更新 PA4 / PA5。 */
    DAC[DAC_CR / 4] = DAC_CR_EN1 | DAC_CR_EN2;

    /* 先输出静音电平，避免上电瞬间 PA4/PA5 悬空被功放放大成"砰"一声 */
    DAC[DAC_DHR12RD / 4] = ((uint32_t)0x0800u << 16) | 0x0800u;
}

/* -------------------------------------------------------------- I2S2 初始化 */

static void i2s2_slave_rx_init(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_SPI2EN;

    /* 1) 先确保关闭，再改配置 */
    SPI2[SPI_I2SCFGR / 4] = 0;
    SPI2[SPI_CR1 / 4] = 0;

    /* 2) CR1 只设 SSM（软件管理 NSS），SSI 保持 0。
      ★ 为什么必须设 SSM：SPI2 的硬件 NSS 脚恰好也是 PB12，而 PB12 在 I2S2
        模式下是 WS(LRCK)。如果让硬件 NSS 生效，LRCK 为高的那一半帧（右声道）
        就会被当成"片选未选中"，右声道直接收不到。
        设 SSM=1 后 NSS 由内部 SSI 决定；SSI=0 表示从机始终被选中，
        接收不受 PB12 电平影响。I2S 模式下 NSS 逻辑本身也是旁路掉的，
        这样设是双保险。 */
    SPI2[SPI_CR1 / 4] = SPI_CR1_SSM;

    /* 3) I2S 配置字：从机接收 + Philips + CK 空闲低 + 16bit 数据 / 32bit 通道 */
    SPI2[SPI_I2SCFGR / 4] = I2S_I2SMOD
                          | I2S_I2SCFG_SLAVERX
                          | I2S_I2SSTD_PHILIPS
                          | I2S_CKPOL_LOW
                          | I2S_DATLEN_16
                          | I2S_CHLEN_32;

    /* 4) I2SPR 在从机模式下不起作用（位时钟来自 CK 脚），给个复位值即可 */
    SPI2[SPI_I2SPR / 4] = 0x0002u;

    /* 5) 中断：RXNE（收满半个字）+ ERRIE（用于抓 OVR 溢出） */
    SPI2[SPI_CR2 / 4] = SPI_CR2_RXNEIE | SPI_CR2_ERRIE;

    /* 6) 使能 I2S。必须在配置写完之后单独置位。 */
    SPI2[SPI_I2SCFGR / 4] |= I2S_I2SE;

    /* 7) NVIC：SPI2_IRQn = 36 */
    NVIC_ISER1 = (1u << (36u - 32u));
}

/* -------------------------------------------------------------- I2S2 中断 */

/* 16 位有符号 -> 12 位无符号（DAC 是单电源 0..Vref，静音在 0x800） */
static inline uint16_t s16_to_dac12(uint16_t v)
{
    int32_t s = (int32_t)(int16_t)v;      /* -32768 .. 32767 */
#if VOL_SHIFT
    s >>= VOL_SHIFT;
#endif
    s += 32768;                            /* 0 .. 65536 */
    s >>= 4;                               /* 0 .. 4096 */
    if (s < 0)     s = 0;
    if (s > 4095)  s = 4095;
    return (uint16_t)s;
}

void SPI2_IRQHandler(void)
{
    uint32_t sr;

    /* OVR 的处理顺序是手册指定的：先读 SR，再读 DR，标志才会清掉。
       顺序反了会一直卡在中断里。 */
    if (SPI2[SPI_SR / 4] & SPI_SR_OVR) {
        (void)SPI2[SPI_DR / 4];
        (void)SPI2[SPI_SR / 4];
        g_ovr++;
        return;
    }

    sr = SPI2[SPI_SR / 4];
    if (sr & SPI_SR_RXNE) {
        int      side = (int)((sr >> 2) & 1u);    /* CHSIDE：0 = 左，1 = 右 */
        uint16_t v    = (uint16_t)SPI2[SPI_DR / 4];

        /* CHSIDE 跳变门控：见文件头的说明。
           同一个声道下重复来的半字是 16bit-extended 的补零段，丢掉。 */
        if (side != g_side) {
            g_side = side;
            if (side == 0) {
                g_left = v;                        /* 左声道先到 */
            } else {
                uint16_t l = g_left;
#if SWAP_LR
                /* 左右对调：把左声道的样本送给 DAC2 */
                DAC[DAC_DHR12RD / 4] = ((uint32_t)s16_to_dac12(l) << 16)
                                     | (uint32_t)s16_to_dac12(v);
#else
                /* DHR12RD：低 12 位 = CH1(PA4) = 左；[27:16] = CH2(PA5) = 右 */
                DAC[DAC_DHR12RD / 4] = ((uint32_t)s16_to_dac12(v) << 16)
                                     | (uint32_t)s16_to_dac12(l);
#endif
                g_right = v;
                g_frames++;
            }
        } else {
            g_dummy++;
        }
    }
}

/* -------------------------------------------------------------- SysTick */

void SysTick_Handler(void)
{
    g_ms++;
}

/* -------------------------------------------------------------- 主程序 */

int main(void)
{
    uint32_t sysclk = clock_init();
    uint32_t last_report = 0;
    uint32_t last_frames = 0;
    uint32_t last_dummy  = 0;
    uint32_t last_ovr    = 0;
    uint32_t blink       = 0;

    gpio_init();
    uart_init(sysclk);      /* USART1 挂在 APB2，此处 APB2 不分频 = sysclk */
    dac_init();
    i2s2_slave_rx_init();

    /* SysTick 1 kHz */
    STK_LOAD = (sysclk / 1000u) - 1u;
    STK_VAL  = 0;
    STK_CTRL = STK_CTRL_CLKSOURCE | STK_CTRL_TICKINT | STK_CTRL_ENABLE;

    uart_puts("\r\n=== STM32F103 I2S2 从机 -> 双通道 DAC ===\r\n");
    uart_puts("SYSCLK "); uart_put_dec(sysclk / 1000000u); uart_puts(" MHz\r\n");
    uart_puts("等待 FPGA 送时钟 ...\r\n");

    for (;;) {
        uint32_t now = g_ms;
        if (now - last_report >= 500u) {
            uint32_t f  = g_frames;
            uint32_t d  = g_dummy;
            uint32_t o  = g_ovr;
            uint32_t df = f - last_frames;

            if (f == 0u && now > 3000u) {
                uart_puts("!! 一帧都没收到：检查 BCLK/LRCK/DATA 三根线与 GND 共地\r\n");
            } else {
                uart_puts("fs=");
                uart_put_dec(df * 2u);        /* 500 ms 帧数 × 2 = 每秒帧数 */
                uart_puts(" Hz  L=");
                uart_put_hex16(g_left);
                uart_puts(" R=");
                uart_put_hex16(g_right);
                uart_puts("  dummy=");
                uart_put_dec(d - last_dummy);
                uart_puts("  ovr=");
                uart_put_dec(o - last_ovr);
                uart_puts("\r\n");
            }

            last_report = now;
            last_frames = f;
            last_dummy  = d;
            last_ovr    = o;

            /* LED 每 500 ms 翻一次：链路在跑就能看到它闪 */
            blink ^= 1u;
            if (blink)
                GPIOC[GPIO_BRR  / 4] = (1u << 13);    /* 亮（低有效） */
            else
                GPIOC[GPIO_BSRR / 4] = (1u << 13);    /* 灭 */
        }
    }
}

/* ============================================================================
 *  关于启动文件
 *  --------------
 *  本文件只提供 main() 与两个中断服务函数（名字用的是 CMSIS 标准名，
 *  会自动覆盖启动文件里的 weak 定义）：
 *      SPI2_IRQHandler     I2S2 接收中断
 *      SysTick_Handler     1 ms 时基
 *  向量表请用工具链自带的启动文件：
 *      Keil  : startup_stm32f10x_md.s（器件选 STM32F103C8）
 *      GCC   : 任意 CMSIS 启动文件 + stm32f103c8.ld
 *  刻意不在这里自己放向量表 —— 一是各编译器语法不通用（weak/alias/section
 *  全是编译器扩展），二是会和自带启动文件重复定义。
 * ==========================================================================*/
