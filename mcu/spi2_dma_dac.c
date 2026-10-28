/* ============================================================================
 *  spi2_dma_dac.c —— STM32F103C8T6：把 FPGA 变声后的音频推到音响（可靠版）
 *
 *  为什么重写（原 i2s_stereo_dac.c 的硬伤）
 *  ------------------------------------------------------------------
 *  0) 【最致命，也是最容易被忽略的一条】从来没人配过系统时钟。
 *     芯片复位后默认跑 HSI 8 MHz，而 TIM6 的 ARR 是按 72 MHz 算的 ——
 *     实际输出帧率变成 8e6/2949 ≈ 2.7 kHz，整整慢了 9 倍。表现就是
 *     "能出声但完全听不清/像噪音"，而且怎么改音频参数都没用。
 *     本文件自带 clock_init()：HSE(8 MHz) ×9 → 72 MHz；HSE 起不来就
 *     退回 HSI/2 ×16 = 64 MHz 并自动改用对应的 ARR。
 *
 *  1) 中断太密。原来每收到半个字进一次 ISR，字率 48.8 k/s、每字 2 次 RXNE
 *     → 约 97.6 k 次中断/秒。F103 @72 MHz 光进出栈就吃掉一大截 CPU，
 *     再叠加串口打印就必然 OVR（溢出丢字），表现就是声音断续、偶发爆音。
 *     现在 SPI 接收走 DMA 循环缓冲，只有"缓冲半满/全满"才进 ISR：
 *     缓冲 512 字 → 约 95 次/秒。
 *
 *  2) 左右声道没有硬同步。原来靠"第一个字是左、之后交替"推断，
 *     一旦丢一个字就**永久左右颠倒**，而且复位也未必能救回来。
 *     现在 CS̄ 是每帧（左+右）一个下降沿（rtl/spi_mirror_tx.v 已同步改），
 *     用 EXTI12 抓这个边沿做硬同步：丢字只烂当前一帧，不会累积。
 *
 *  3) 速率漂移没处理。FPGA 的时钟来自板上 50 MHz 晶振，STM32 来自自己的
 *     晶振，两者差几十到几百 ppm。原来没有任何缓冲水位调节，长时间跑必然
 *     周期性欠载/溢出（每隔几分钟"咔"一下）。现在按缓冲水位做
 *     **整帧对齐**的丢帧/重复帧自适应，把漂移吸收在单个 41 µs 的帧里。
 *
 *  硬件连接（与 rtl/spi_mirror_tx.v 文件头一致）
 *  ------------------------------------------------------------------
 *      FPGA A13 (mcu_sclk) -> PB13  SPI2_SCK
 *      FPGA A14 (mcu_csn)  -> PB12  EXTI12 帧同步（软件 NSS 常选）
 *      FPGA A15 (mcu_mosi) -> PB15  SPI2_MOSI
 *      GND                 -> GND   （不共地数据全是乱码，最常见的"没声音"）
 *      PA4 = DAC1 = 左声道   PA5 = DAC2 = 右声道
 *        → 两级 RC 低通（4k7+2n2 / 10k+1n）→ 10 µF 隔直 → PAM8403
 *
 *  为什么用 SPI2 而不是 SPI1：SPI1 占 PA4/PA5/PA6/PA7，正好和 DAC1/DAC2
 *  打架；SPI2 在 PB12/13/15，把 PA4/PA5 完整留给 DAC。
 *
 *  编译与验证
 *  ------------------------------------------------------------------
 *    arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -ffreestanding ...
 *    本机没有 ARM 编译器，逻辑正确性由 tool/verify_mcu_dac_driver.py
 *    把本文件的核心算法移植到 Python、喂真实波形逐样本验证。
 * ==========================================================================*/

/* ---- 编译期开关 ---- */
#ifndef CS_PER_FRAME
#define CS_PER_FRAME   1     /* 1 = CS̄ 每帧一个下降沿（新 FPGA，推荐）
                                0 = CS̄ 每字一个下降沿（旧 FPGA 兼容模式，
                                    只能靠交替推断左右，丢字会左右颠倒） */
#endif
#ifndef RB_WORDS
#define RB_WORDS       512   /* 环形缓冲字数（必须是 2 的幂，且 >= 64） */
#endif
#define RB_MASK        (RB_WORDS - 1)
#define RB_HALF        (RB_WORDS / 2)
#define RB_BAND        (RB_WORDS / 8)     /* 水位允许的偏离带（±64 字） */

/* 时钟配置：绝大多数 F103 最小系统板焊的是 8 MHz 晶振，×9 = 72 MHz。
   如果你的板子是别的频率，改这两个宏即可（PLLMUL 见下面 clock_init 注释）。 */
#ifndef HSE_MHZ
#define HSE_MHZ        8
#endif
#ifndef PLL_MUL
#define PLL_MUL        9                  /* 8 MHz × 9 = 72 MHz */
#endif
#define TIM_CLK_HZ     ((unsigned long)(HSE_MHZ) * (unsigned long)(PLL_MUL) * 1000000UL)
#define FS_OUT_HZ      24414u             /* FPGA 的 FS_OUT = 50 MHz / 2048 */
/* ARR = TIM_CLK / FS - 1。72 MHz 下 = 2948，此时实际 24415.1 Hz，
   与 FPGA 只差 +41 ppm（约每 1 秒需要补一帧，听不出来）。
   ★ 注意别写成 2949：那是 24406.8 Hz，差 -298 ppm，每秒要补 7 帧。 */
#define TIM_ARR_NOM    ((unsigned int)(TIM_CLK_HZ / (unsigned long)FS_OUT_HZ) - 1u)

/* 上电自检提示音：让"没声音"这件事能被二分定位。
   1 = 上电先"嘀"一声（0.25 s，1 kHz，左右同相）；
      响了 = DAC + RC 低通 + 功放 + 音响这条链路是好的，问题在 SPI 输入；
      不响 = 先别管 FPGA，去查 PA4/PA5、功放供电、音量。
   0 = 关掉（正式跑起来建议关，免得每次上电都响）。 */
#ifndef DIAG_BEEP
#define DIAG_BEEP       1
#endif
/* 连续 3 秒收不到 SPI 数据就再"嘀"一声：区分"FPGA 没送"和"DAC 坏了" */
#ifndef DIAG_NODATA_BEEP
#define DIAG_NODATA_BEEP 1
#endif
#define DIAG_PH_INC     2684u   /* 1 kHz 三角波在 24414 Hz 下的相位步进（Q16） */
#define DIAG_BEEP_SHORT 3662u   /* 0.15 s */
#define DIAG_BEEP_START 6104u   /* 0.25 s */
#define DIAG_AMP        9000    /* 提示音幅度（s16，约 -11 dBFS） */

typedef volatile unsigned int reg32_t;

#define RCC_BASE   0x40021000u
#define FLASH_BASE 0x40022000u
#define AFIO_BASE  0x40010000u
#define EXTI_BASE  0x40010400u
#define GPIOA_BASE 0x40010800u
#define GPIOB_BASE 0x40010C00u
#define SPI2_BASE  0x40003800u
#define DMA1_BASE  0x40020000u
#define TIM6_BASE  0x40001000u
#define DAC_BASE   0x40007400u
#define NVIC_BASE  0xE000E100u

#define RCC   ((reg32_t *)RCC_BASE)
#define FLASH ((reg32_t *)FLASH_BASE)
#define AFIO  ((reg32_t *)AFIO_BASE)
#define EXTI  ((reg32_t *)EXTI_BASE)
#define GPIOA ((reg32_t *)GPIOA_BASE)
#define GPIOB ((reg32_t *)GPIOB_BASE)
#define SPI2  ((reg32_t *)SPI2_BASE)
#define DMA1  ((reg32_t *)DMA1_BASE)
#define TIM6  ((reg32_t *)TIM6_BASE)
#define DAC   ((reg32_t *)DAC_BASE)
#define NVIC  ((reg32_t *)NVIC_BASE)

/* RCC */
#define RCC_CR        (0x00u / 4u)
#define RCC_CFGR      (0x04u / 4u)
#define RCC_APB2ENR   (0x18u / 4u)
#define RCC_APB1ENR   (0x1Cu / 4u)
#define IO_PA_EN      (1u << 2)
#define IO_PB_EN      (1u << 3)
#define AFIO_EN       (1u << 0)
#define SPI2_EN       (1u << 14)
#define TIM6_EN       (1u << 4)
#define DAC_EN        (1u << 29)

#define RCC_CR_HSION   (1u << 0)
#define RCC_CR_HSIRDY  (1u << 1)
#define RCC_CR_HSEON   (1u << 16)
#define RCC_CR_HSERDY  (1u << 17)
#define RCC_CR_PLLON   (1u << 24)
#define RCC_CR_PLLRDY  (1u << 25)

#define FLASH_ACR     (0x00u / 4u)
#define FLASH_PRFTBE  (1u << 4)

/* SPI2 */
#define SPI_CR1  (0x00u / 4u)
#define SPI_CR2  (0x04u / 4u)
#define SPI_SR   (0x08u / 4u)
#define SPI_DR   (0x0Cu / 4u)
#define SPI_CR1_DFF     (1u << 11)   /* 16 bit 数据帧 */
#define SPI_CR1_SSM     (1u << 9)
#define SPI_CR1_SSI     (1u << 8)
#define SPI_CR1_SPE     (1u << 6)
#define SPI_CR2_RXDMAEN (1u << 0)
#define SPI_SR_OVR      (1u << 6)

/* DMA1（通道间隔 0x14 字节 = 5 个 32bit 寄存器） */
#define DMA_ISR    (0x00u / 4u)
#define DMA_IFCR   (0x04u / 4u)
#define DMA_CH4_CCR   ((0x40u + 0x00u) / 4u)
#define DMA_CH4_CNDTR ((0x40u + 0x04u) / 4u)
#define DMA_CH4_CPAR  ((0x40u + 0x08u) / 4u)
#define DMA_CH4_CMAR  ((0x40u + 0x0Cu) / 4u)
#define DMA_CCR_EN      (1u << 0)
#define DMA_CCR_TCIE    (1u << 1)
#define DMA_CCR_HTIE    (1u << 2)
#define DMA_CCR_CIRC    (1u << 5)
#define DMA_CCR_MINC    (1u << 7)
#define DMA_CCR_PSIZE16 (1u << 9)
#define DMA_CCR_MSIZE16 (1u << 11)
#define DMA_CCR_PL_HIGH (2u << 12)
#define DMA_IFCR_CH4    (0xFu << 12)
/* DMA_ISR 里 channel4 的四个标志（GIF4=12 / TCIF4=13 / HTIF4=14 / TEIF4=15） */
#define DMA_TCIF4       (1u << 13)
#define DMA_HTIF4       (1u << 14)
#define DMA_TEIF4       (1u << 15)

/* TIM6 */
#define TIM_CR1  (0x00u / 4u)
#define TIM_DIER (0x0Cu / 4u)
#define TIM_SR   (0x10u / 4u)
#define TIM_PSC  (0x28u / 4u)
#define TIM_ARR  (0x2Cu / 4u)
#define TIM_CR1_CEN  (1u << 0)
#define TIM_DIER_UIE (1u << 0)
#define TIM_SR_UIF   (1u << 0)

/* DAC */
#define DAC_CR      (0x00u / 4u)
#define DAC_DHR12RD (0x20u / 4u)
#define DAC_CR_EN1  (1u << 0)
#define DAC_CR_EN2  (1u << 16)

/* EXTI / AFIO */
#define EXTI_IMR   (0x00u / 4u)
#define EXTI_FTSR  (0x0Cu / 4u)
#define EXTI_PR    (0x14u / 4u)
/* EXTI12 挂在 AFIO_EXTICR4 的 [3:0]（EXTICR1..4 分别管 EXTI0..15） */
#define AFIO_EXTICR4 (0x14u / 4u)

/* NVIC ISER0 覆盖 IRQ 0..31，ISER1 覆盖 IRQ 32..63 */
#define NVIC_ISER0     (0x00u / 4u)
#define NVIC_ISER1     (0x04u / 4u)
#define IRQ_EXTI15_10  40
#define IRQ_TIM6       54
#define IRQ_DMA1_CH4   14

/* ------------------------------------------------------------------ 状态 */

static volatile unsigned short g_dma_buf[RB_WORDS];   /* DMA 循环写入 */
static volatile unsigned int   g_wr = 0;              /* 已接收字数（单调增） */
static volatile unsigned int   g_rd = 0;              /* 已消费字数（单调增） */
static volatile unsigned short g_left  = 0x0800;
static volatile unsigned short g_right = 0x0800;
static volatile unsigned int   g_ovr = 0;             /* SPI 溢出次数 */
static volatile unsigned int   g_resync = 0;          /* 帧重新同步次数 */
static volatile unsigned int   g_under = 0;           /* 缓冲空（欠载）次数 */
static volatile unsigned int   g_over  = 0;           /* 缓冲满（溢出）次数 */
static volatile unsigned int   g_hold  = 0;           /* 因水位不足而保持上一帧的次数 */
static volatile unsigned int   g_drop  = 0;           /* 追不上时丢掉的帧数 */
static volatile unsigned int   g_phase = 0;           /* 当前字是左(0)还是右(1) */
static volatile unsigned int   g_phase_valid = 0;     /* 相位是否已同步上 */
static volatile unsigned int   g_frame_anchor = 0;    /* 最近一次 CS̄ 下降沿时的写指针 */
static volatile unsigned int   g_cycles = 0;          /* DMA 跑完的整轮数（单调增） */
static volatile unsigned int   g_te   = 0;            /* DMA 传输错误次数 */
#if DIAG_BEEP || DIAG_NODATA_BEEP
static volatile unsigned int   g_diag_left = 0;       /* 剩余提示音帧数 */
static volatile unsigned int   g_diag_phase = 0;      /* 1 kHz 三角波相位（Q16） */
#endif
#if DIAG_NODATA_BEEP
static volatile unsigned int   g_idle = 0;            /* 连续无数据的帧数 */
#endif
static volatile unsigned int   g_last_wr = 0;

/* 提示音波形：Q16 相位 -> 三角波，避免为"嘀一声"再塞一张正弦表 */
static int diag_wave(unsigned int ph, int amp)
{
    unsigned int h = ph & 0x7FFFu;        /* 半周期内的斜坡 0..32767 */
    int v = ((int)h - 16384) << 1;        /* -32768..32766 */
    if (ph & 0x8000u) {
        v = -v;
    }
    /* ★ 先乘后除：写成 v/32768*amp 的话整数除法先塌成 0 或 -1，整段没声 */
    return v * amp / 32768;
}

/* s16（有符号 16bit）-> 12bit 无符号 DAC 码 */
static unsigned short s16_to_dac(int s)
{
    unsigned int u = (unsigned int)(s + 32768);       /* 0..65535 */
    u >>= 4;                                          /* 0..4095 */
    if (u > 4095u) {
        u = 4095u;
    }
    return (unsigned short)u;
}

/* 打包成 DHR12RD：低 12 位 = CH1(PA4,左)，[27:16] = CH2(PA5,右) */
static unsigned int pack_dhr12rd(unsigned short l, unsigned short r)
{
    return ((unsigned int)l & 0x0FFFu) | (((unsigned int)r & 0x0FFFu) << 16);
}

/* ---------------------------------------------------------- 环形缓冲读写 */

/* 缓冲里还有多少个字没被消费 */
static int rb_level(void)
{
    return (int)(g_wr - g_rd);
}

/* 取一个字；缓冲空返回静音并记欠载 */
static unsigned short rb_pop(void)
{
    if (rb_level() <= 0) {
        g_under++;
        return 0x8000u;                    /* 静音（s16 的 0） */
    }
    return g_dma_buf[g_rd & RB_MASK];
}

static void rb_advance(void)
{
    if (rb_level() > 0) {
        g_rd++;
    }
}

/* ------------------------------------------------------------------ 初始化 */

/* ★ 系统时钟。没有这一段，芯片就跑在复位默认的 HSI 8 MHz 上，
   TIM6 的输出帧率会是 2.7 kHz 而不是 24.4 kHz —— "有声音但完全听不清"
   的第一嫌疑就是它。 */
static void clock_init(void)
{
    unsigned int cfgr;
    unsigned int mul;

    /* 已经切到 PLL 了就别再折腾（避免重复调用把时钟搞断） */
    if (((RCC[RCC_CFGR] >> 2) & 0x3u) == 0x2u) {
        return;
    }

    RCC[RCC_CR] |= RCC_CR_HSEON;
    {
        unsigned int guard = 0;
        while (!(RCC[RCC_CR] & RCC_CR_HSERDY) && guard < 0x100000u) {
            guard++;
        }
    }

    if (RCC[RCC_CR] & RCC_CR_HSERDY) {
        /* HSE 可用：PLLSRC = HSE，不分频，×PLL_MUL */
        RCC[RCC_CFGR] |= (1u << 16);
        RCC[RCC_CFGR] &= ~(1u << 17);
        mul = (unsigned int)(PLL_MUL - 2);        /* PLLMUL 编码：0=×2 … 7=×9 */
    } else {
        /* HSE 起不来（没焊晶振 / 焊了别的频率）：退回 HSI/2 = 4 MHz，
           ×16 = 64 MHz。TIM_ARR_NOM 是宏，改 PLL_MUL 一起就跟着变。 */
        RCC[RCC_CR] &= ~RCC_CR_HSEON;
        RCC[RCC_CR] |= RCC_CR_HSION;
        while (!(RCC[RCC_CR] & RCC_CR_HSIRDY)) {
        }
        RCC[RCC_CFGR] &= ~(1u << 16);             /* PLLSRC = HSI/2 */
        mul = 14u;                                /* 0b1110 = ×16 */
    }

    cfgr = RCC[RCC_CFGR];
    cfgr &= ~(0xFu << 18);   cfgr |= (mul << 18);   /* PLLMUL */
    cfgr &= ~(0xFu << 4);                           /* HPRE  = /1  (HCLK=SYSCLK) */
    cfgr &= ~(0x7u << 8);    cfgr |= (4u << 8);     /* PPRE1 = /2  (PCLK1=36M) */
    cfgr &= ~(0x7u << 11);                          /* PPRE2 = /1  (PCLK2=72M) */
    cfgr &= ~(0x3u << 14);   cfgr |= (2u << 14);    /* ADCPRE= /6  (12 MHz) */
    RCC[RCC_CFGR] = cfgr;

    RCC[RCC_CR] |= RCC_CR_PLLON;
    while (!(RCC[RCC_CR] & RCC_CR_PLLRDY)) {
    }

    FLASH[FLASH_ACR] = FLASH_PRFTBE | 0x2u;         /* 预取 + 2 个等待周期 */
    RCC[RCC_CFGR] = (RCC[RCC_CFGR] & ~0x3u) | 0x2u; /* SW = PLL */
    while (((RCC[RCC_CFGR] >> 2) & 0x3u) != 0x2u) {
    }
}

static void gpio_init(void)
{
    /* PA4/PA5 = DAC 输出，模拟输入模式（MODE=00, CNF=00） */
    GPIOA[0x00u / 4u] &= ~((0xFu << 16) | (0xFu << 20));
    /* PB12 = 浮空输入（CS̄，EXTI12）；PB13 = 浮空输入（SCK）；
       PB15 = 浮空输入（MOSI）。复位后默认就是浮空输入，这里显式写一遍。
       （SPI 从机的 SCK/MOSI 就是浮空输入，不要配成复用推挽。） */
    GPIOB[0x04u / 4u] &= ~((0xFu << 16) | (0xFu << 20) | (0xFu << 28));
    GPIOB[0x04u / 4u] |=  ((0x4u << 16) | (0x4u << 20) | (0x4u << 28));
}

static void spi2_slave_dma_init(void)
{
    /* SPI2：从机、16bit、模式 0,0、软件 NSS 常选（SSI=1 让从机一直被选中） */
    SPI2[SPI_CR1] = SPI_CR1_DFF | SPI_CR1_SSM | SPI_CR1_SSI;
    SPI2[SPI_CR2] = SPI_CR2_RXDMAEN;
    SPI2[SPI_CR1] |= SPI_CR1_SPE;

    /* DMA1_Channel4 = SPI2_RX，循环模式，16bit→16bit，存储器递增 */
    DMA1[DMA_CH4_CPAR]  = (unsigned int)(SPI2_BASE + 0x0Cu);   /* -> SPI2->DR */
    DMA1[DMA_CH4_CMAR]  = (unsigned int)&g_dma_buf[0];
    DMA1[DMA_CH4_CNDTR] = RB_WORDS;
    DMA1[DMA_CH4_CCR]   = DMA_CCR_MINC | DMA_CCR_PSIZE16 | DMA_CCR_MSIZE16
                        | DMA_CCR_CIRC | DMA_CCR_HTIE | DMA_CCR_TCIE
                        | DMA_CCR_PL_HIGH;
    DMA1[DMA_CH4_CCR]  |= DMA_CCR_EN;

    NVIC[NVIC_ISER0] = (1u << (IRQ_DMA1_CH4 & 31));
}

static void exti_frame_sync_init(void)
{
    /* EXTI12 <- PB12（EXTICR4[3:0] = 0001 = Port B） */
    AFIO[AFIO_EXTICR4] = (AFIO[AFIO_EXTICR4] & ~0xFu) | 0x1u;
    EXTI[EXTI_FTSR] |= (1u << 12);                              /* 下降沿 */
    EXTI[EXTI_IMR]  |= (1u << 12);
    NVIC[NVIC_ISER1] = (1u << (IRQ_EXTI15_10 & 31));
}

static void dac_tim6_init(void)
{
    DAC[DAC_CR] |= DAC_CR_EN1 | DAC_CR_EN2;

    /* TIM6：TIM_CLK / (PSC+1) / (ARR+1) = 24414 Hz
       ★ 时钟由 clock_init() 保证是 72 MHz，这里 ARR 才成立。 */
    TIM6[TIM_PSC] = 0;
    TIM6[TIM_ARR] = TIM_ARR_NOM;
    TIM6[TIM_DIER] = TIM_DIER_UIE;
    TIM6[TIM_CR1] = TIM_CR1_CEN;
    NVIC[NVIC_ISER1] = (1u << (IRQ_TIM6 & 31));
}

/* -------------------------------------------------------------- 中断处理 */

/* ★ 写指针必须"随要随算"，不能只在 DMA 中断里推进。

   踩过的两个坑：

   坑一（只数半缓冲）：g_wr = g_half * RB_HALF，只在 HT/TC 中断里加一。
      于是 g_wr 每 5.24 ms 才跳一次 256 字，而消费者是连续取的 ——
      rb_level() 画出一条"跳到 256 再线性掉到 0"的锯齿，每个半周期末尾
      都被误判成"水位不足"从而保持上一帧，听感就是规律性的断续。

   坑二（从 CNDTR 反推，但 base 取自旧的 g_wr）：漏掉一次 TC 时 base 还
      停在上一轮，算出来的 g_wr 会往回跳，rb_level() 直接变负。

   现在的做法：中断只维护"跑完几整轮" g_cycles（TCIF4 是锁存标志，一轮
   一次，漏不掉也多不了），真正的字位置在**用的时候**才结合 CNDTR 现算：

       g_wr = g_cycles * RB_WORDS + (RB_WORDS - CNDTR)

   关于竞争：TCIF4 已置位、中断还没服务时读 CNDTR，CNDTR 已被硬件重装成
   RB_WORDS，此时 (RB_WORDS - CNDTR) = RB_WORDS，恰好就是"本轮写满"的
   位置，与 g_cycles 尚未 +1 正好抵消 —— 算出来仍然是对的。
   另外所有 IRQ 都是默认优先级 0，Cortex-M3 同优先级不互相抢占，所以
   TIM6 里这段连读不会被 DMA 中断打断。 */
static unsigned int dma_wr_now(void)
{
    unsigned int rem  = DMA1[DMA_CH4_CNDTR] & (unsigned int)RB_MASK;
    unsigned int done = (RB_WORDS - rem) & (unsigned int)RB_MASK;
    return g_cycles * RB_WORDS + done;
}

void DMA1_Channel4_IRQHandler(void)
{
    unsigned int isr = DMA1[DMA_ISR];
    if (isr & (DMA_TCIF4 | DMA_HTIF4 | DMA_TEIF4)) {
        if (isr & DMA_TEIF4) {
            g_te++;
        }
        if (isr & DMA_TCIF4) {
            g_cycles++;                               /* 跑完整整一轮 */
        }
        DMA1[DMA_IFCR] = DMA_IFCR_CH4;                /* 四个标志一起清 */
        g_wr = dma_wr_now();
        /* 消费严重跟不上 → 丢掉最旧的半缓冲，宁可跳一小段
           也不要让延迟越积越大 */
        if (rb_level() > (int)RB_WORDS - 8) {
            g_over++;
            g_rd = g_wr - RB_HALF;
        }
    }
}

/* CS̄ 下降沿 = 帧起点 = 左声道字的起点 */
void EXTI15_10_IRQHandler(void)
{
    if (EXTI[EXTI_PR] & (1u << 12)) {
        EXTI[EXTI_PR] = (1u << 12);                   /* 写 1 清 */
#if CS_PER_FRAME
        if (g_phase != 0) {
            g_resync++;
        }
        g_phase = 0;
        g_phase_valid = 1;
        g_wr = dma_wr_now();
        g_frame_anchor = g_wr;
        /* ★ 真正的恢复动作在这里，只重置相位标记是不够的：
           CS̄ 下降沿 == "FPGA 接下来要发的是左声道"，而已收到的最后一个
           字应当是上一帧的右声道 —— 所以 (已收 - 已消费) 必须是**偶数**。
           是奇数就说明中途丢过字，丢掉一个凑成偶数，下一帧立刻正确。
           （只置相位标记的话，读写速率仍是一帧两个字，错位会一直持续：
             tool/verify_mcu_dac_driver.py 实测那样错帧 293/379。） */
        if (((g_wr - g_rd) & 1u) && (g_wr > g_rd)) {
            g_rd++;
            g_resync++;
        }
#else
        /* 旧 FPGA：每个字一个下降沿，只能交替推断，丢字就会左右颠倒 */
        g_phase ^= 1u;
        g_phase_valid = 1;
#endif
    }
}

/* TIM6：每 1/fs 触发一次，一次写满左右两个 DAC 通道 */
void TIM6_IRQHandler(void)
{
    int level;

    if (!(TIM6[TIM_SR] & TIM_SR_UIF)) {
        return;
    }
    TIM6[TIM_SR] = ~TIM_SR_UIF;                       /* 写 0 清 */

    /* SPI 溢出：读 SR 再读 DR 才能清掉，顺序是手册指定的 */
    if (SPI2[SPI_SR] & SPI_SR_OVR) {
        (void)SPI2[SPI_DR];
        g_ovr++;
        g_phase_valid = 0;                            /* 相位已不可信，等下一次 CS̄ 同步 */
    }

    g_wr = dma_wr_now();                              /* ★ 每次都用最新值 */

#if DIAG_NODATA_BEEP
    /* 收不到数据就周期性"嘀"一声：一眼分清是"FPGA 没送"还是"DAC 没出" */
    if (g_wr != g_last_wr) {
        g_last_wr = g_wr;
        g_idle = 0;
    } else if (g_idle < 0xFFFFFFFFu) {
        g_idle++;
        if (g_idle > (3u * 24414u)) {
            g_idle = 0;
            g_diag_left = DIAG_BEEP_SHORT;
        }
    }
#endif

#if DIAG_BEEP || DIAG_NODATA_BEEP
    /* 提示音期间照响不误（上电自检就是要能听见）。期间环形缓冲会溢出一轮，
       靠 CS̄ 帧同步自动对齐回来，不会留下后遗症。 */
    if (g_diag_left) {
        g_diag_left--;
        g_diag_phase = (g_diag_phase + DIAG_PH_INC) & 0xFFFFu;
        {
            int v = diag_wave(g_diag_phase, DIAG_AMP);
            g_left  = s16_to_dac(v);
            g_right = s16_to_dac(v);
        }
        DAC[DAC_DHR12RD] = pack_dhr12rd(g_left, g_right);
        return;
    }
#endif

    level = rb_level();

    /* ---- 速率自适应：丢帧 / 重复帧，但必须**整帧对齐** ----
       两侧晶振差几十到几百 ppm，MCU 的 TIM6 只能给到 ~340 ppm 一档的
       分辨率（72 MHz / ARR ≈ 8.3 Hz/档），剩下的残差必须靠动数据吸收。

       ★ 关键：一动就是 2 个字（一整帧 L+R）。只丢/只补 1 个字会把左右
         声道的相位打翻，那才是"声音忽左忽右"的真正来源（旧驱动就是这么
         坏的，tool/verify_mcu_dac_driver.py 的负对照专门盯这条）。

       水位被这条规则钉在 mid±(RB_BAND+2) 的窄带里：越界就每帧纠一次，
       一次纠 2 个字，所以只会在这条带边上抖 ±2 字。代价是每 0.04~1 s
       丢/补一个 41 µs 的帧，对音乐和语音都听不出来。 */
    if (level > (int)(RB_HALF + RB_BAND)) {
        rb_advance();                                 /* 多消费一个字 */
        rb_advance();
        g_drop++;
        level -= 2;
    } else if (level < (int)(RB_HALF - RB_BAND)) {
        /* 水位太低：本帧不消费（保持上一帧），让缓冲回血 */
        g_hold++;
        DAC[DAC_DHR12RD] = pack_dhr12rd(g_left, g_right);
        return;
    }

    if (level >= 2) {
        unsigned short w0 = rb_pop();  rb_advance();
        unsigned short w1 = rb_pop();  rb_advance();
#if CS_PER_FRAME
        /* 有锚点就按锚点判左右：锚点之后的第 0 个字是左 */
        if (g_phase_valid && ((g_rd - g_frame_anchor) & 1u)) {
            unsigned short t = w0; w0 = w1; w1 = t;   /* 奇数相位说明错开了一个字 */
        }
#endif
        g_left  = s16_to_dac((short)w0);
        g_right = s16_to_dac((short)w1);
    } else {
        /* 真没数据：保持上一帧的样本（不写 0，避免"咔"的一声） */
        g_hold++;
    }

    DAC[DAC_DHR12RD] = pack_dhr12rd(g_left, g_right);
}

/* ------------------------------------------------------------------ main */

int main(void)
{
    clock_init();

    RCC[RCC_APB2ENR] |= IO_PA_EN | IO_PB_EN | AFIO_EN;
    RCC[RCC_APB1ENR] |= SPI2_EN | TIM6_EN | DAC_EN;

    gpio_init();
    spi2_slave_dma_init();
    exti_frame_sync_init();
#if DIAG_BEEP
    g_diag_left = DIAG_BEEP_START;   /* 上电"嘀"一声，先证明 DAC 到音响是通的 */
#endif
    dac_tim6_init();

    for (;;) {
        /* 主循环什么都不用做：收靠 DMA、出靠 TIM6。
           这里可以放状态灯 / 串口统计打印（g_ovr / g_drop / g_hold …）。 */
        __asm__ volatile ("wfi");
    }
}
