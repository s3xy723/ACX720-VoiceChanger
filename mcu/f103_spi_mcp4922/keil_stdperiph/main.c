/* ============================================================================
 *  main.c —— STM32F103C8T6：FPGA→MCP4922 全 SPI 音频桥（无 I2S 方案）
 *  标准库（StdPeriph V3.5）版本，逻辑与寄存器级 v1.0 完全一致
 *
 *  链路：
 *    FPGA(spi_mirror_tx, 2MHz SPI 主机, 每帧 左/右 两个 16bit 字)
 *      → A13=SCLK→PA5(SPI1_SCK)  A14=CS̄→PA4(SPI1_NSS)  A15=MOSI→PA7(SPI1_MOSI)
 *      → SPI1 从机 16bit 接收（EXTI4 在 PA4 下降沿同步左右声道）
 *      → SPI2 主机 4MHz → MCP4922（PB13=SCK, PB15=SDI, PB12=CS̄, PB0=LDAC̄）
 *      → 两级 RC + 10µF 隔直 → PAM8403 → 2× 4Ω 3W 喇叭
 *
 *  自检音：上电后若 1 秒内没收到任何 FPGA 字，自动进测试模式，
 *  TIM2 以 24.42 kHz 写 440 Hz 正弦（左右同相、幅度约 0.61 V 峰值），
 *  用来在没接 FPGA 时单独验证 DAC→功放→喇叭整条模拟链路。
 *  FPGA 一来数据立即切回直通。
 *
 *  ★ LED 状态指示（Blue Pill 板载 PC13，低有效）——不用串口也能验收：
 *      慢闪（1 Hz 翻转）  = TEST 模式，没收到 FPGA 数据（FPGA 位流没跑/线没接好）
 *      常亮              = PASS 模式，FPGA 数据正在流入（SPI 链路已打通）
 *      快闪后才常亮       = 上电曾进过 TEST，随后收到数据自动切 PASS（正常现象）
 *
 *  时钟：HSI 8MHz ÷2 ×16 = 64 MHz（不依赖板上晶振，任何 C8T6 板都能跑）
 *  串口：PA9(TX) 115200-8-N-1，每 500 ms 打一行状态。
 *
 *  Keil5 工程：见同目录《README_Keil5导入说明.md》
 * ==========================================================================*/
#include "stm32f10x.h"
#include <stdint.h>

/* ------------------------------------------------------------ MCP4922 命令字 */
#define CMD_DACA        0x3000u     /* bit15 A/B=0(DACA) BUF=0 GA=1(1x) SHDN=1 */
#define CMD_DACB        0xB000u     /* bit15 A/B=1(DACB) BUF=0 GA=1(1x) SHDN=1 */

/* ------------------------------------------------------------ 全局状态 */
static volatile uint32_t g_ms;          /* SysTick 毫秒计数 */
static volatile uint32_t g_last_word_ms;/* 最近一次收到 FPGA 字的时刻 */
static volatile uint32_t g_words;       /* 累计收到的 FPGA 字数 */
static volatile uint32_t g_ovr;         /* SPI1 溢出次数（非 0 = 链路跟不上） */
static volatile uint16_t g_left, g_right;
static volatile uint8_t  g_side;        /* 0=下一个字是左声道 1=右声道 */
static volatile uint8_t  g_test_mode;   /* 1 = 无 FPGA 信号，发自检音 */
static uint16_t          g_phase;       /* 自检音相位累加器（16bit） */

/* 256 点 12-bit 正弦表，中心 2048、幅度 1400（≈0.61 V 峰值 @VDD=3.3V） */
static const uint16_t g_sine256[256] = {
    0x800U, 0x822U, 0x845U, 0x867U, 0x889U, 0x8ABU, 0x8CDU, 0x8EFU,
    0x911U, 0x933U, 0x954U, 0x975U, 0x996U, 0x9B7U, 0x9D8U, 0x9F8U,
    0xA18U, 0xA37U, 0xA57U, 0xA75U, 0xA94U, 0xAB2U, 0xAD0U, 0xAEDU,
    0xB0AU, 0xB26U, 0xB42U, 0xB5DU, 0xB78U, 0xB92U, 0xBACU, 0xBC5U,
    0xBDEU, 0xBF6U, 0xC0DU, 0xC24U, 0xC3AU, 0xC50U, 0xC64U, 0xC79U,
    0xC8CU, 0xC9FU, 0xCB1U, 0xCC2U, 0xCD3U, 0xCE3U, 0xCF2U, 0xD00U,
    0xD0DU, 0xD1AU, 0xD26U, 0xD31U, 0xD3CU, 0xD45U, 0xD4EU, 0xD56U,
    0xD5DU, 0xD63U, 0xD69U, 0xD6DU, 0xD71U, 0xD74U, 0xD76U, 0xD78U,
    0xD78U, 0xD78U, 0xD76U, 0xD74U, 0xD71U, 0xD6DU, 0xD69U, 0xD63U,
    0xD5DU, 0xD56U, 0xD4EU, 0xD45U, 0xD3CU, 0xD31U, 0xD26U, 0xD1AU,
    0xD0DU, 0xD00U, 0xCF2U, 0xCE3U, 0xCD3U, 0xCC2U, 0xCB1U, 0xC9FU,
    0xC8CU, 0xC79U, 0xC64U, 0xC50U, 0xC3AU, 0xC24U, 0xC0DU, 0xBF6U,
    0xBDEU, 0xBC5U, 0xBACU, 0xB92U, 0xB78U, 0xB5DU, 0xB42U, 0xB26U,
    0xB0AU, 0xAEDU, 0xAD0U, 0xAB2U, 0xA94U, 0xA75U, 0xA57U, 0xA37U,
    0xA18U, 0x9F8U, 0x9D8U, 0x9B7U, 0x996U, 0x975U, 0x954U, 0x933U,
    0x911U, 0x8EFU, 0x8CDU, 0x8ABU, 0x889U, 0x867U, 0x845U, 0x822U,
    0x800U, 0x7DEU, 0x7BBU, 0x799U, 0x777U, 0x755U, 0x733U, 0x711U,
    0x6EFU, 0x6CDU, 0x6ACU, 0x68BU, 0x66AU, 0x649U, 0x628U, 0x608U,
    0x5E8U, 0x5C9U, 0x5A9U, 0x58BU, 0x56CU, 0x54EU, 0x530U, 0x513U,
    0x4F6U, 0x4DAU, 0x4BEU, 0x4A3U, 0x488U, 0x46EU, 0x454U, 0x43BU,
    0x422U, 0x40AU, 0x3F3U, 0x3DCU, 0x3C6U, 0x3B0U, 0x39CU, 0x387U,
    0x374U, 0x361U, 0x34FU, 0x33EU, 0x32DU, 0x31DU, 0x30EU, 0x300U,
    0x2F3U, 0x2E6U, 0x2DAU, 0x2CFU, 0x2C4U, 0x2BBU, 0x2B2U, 0x2AAU,
    0x2A3U, 0x29DU, 0x297U, 0x293U, 0x28FU, 0x28CU, 0x28AU, 0x288U,
    0x288U, 0x288U, 0x28AU, 0x28CU, 0x28FU, 0x293U, 0x297U, 0x29DU,
    0x2A3U, 0x2AAU, 0x2B2U, 0x2BBU, 0x2C4U, 0x2CFU, 0x2DAU, 0x2E6U,
    0x2F3U, 0x300U, 0x30EU, 0x31DU, 0x32DU, 0x33EU, 0x34FU, 0x361U,
    0x374U, 0x387U, 0x39CU, 0x3B0U, 0x3C6U, 0x3DCU, 0x3F3U, 0x40AU,
    0x422U, 0x43BU, 0x454U, 0x46EU, 0x488U, 0x4A3U, 0x4BEU, 0x4DAU,
    0x4F6U, 0x513U, 0x530U, 0x54EU, 0x56CU, 0x58BU, 0x5A9U, 0x5C9U,
    0x5E8U, 0x608U, 0x628U, 0x649U, 0x66AU, 0x68BU, 0x6ACU, 0x6CDU,
    0x6EFU, 0x711U, 0x733U, 0x755U, 0x777U, 0x799U, 0x7BBU, 0x7DEU,
};

/* ------------------------------------------------------------ MCP4922 写入 */
/* 原子保护：SPI1 RXNE 与 TIM2（自检音）都会调它，两处 ISR 抢 SPI2 会把
   命令帧拆散，所以整个"CS̄低→16bit→CS̄高"序列必须关中断完成（约 4 µs）。 */
static void dac_write(uint16_t cmd, uint16_t d12)
{
    uint32_t pm = __get_PRIMASK();
    __disable_irq();

    while (SPI_I2S_GetFlagStatus(SPI2, SPI_I2S_FLAG_BSY) == SET) { }
    GPIO_ResetBits(GPIOB, GPIO_Pin_12);                     /* CS̄ = 0 */
    SPI_I2S_SendData(SPI2, (uint16_t)(cmd | (d12 & 0x0FFFu)));
    while (SPI_I2S_GetFlagStatus(SPI2, SPI_I2S_FLAG_TXE) == RESET) { }
    while (SPI_I2S_GetFlagStatus(SPI2, SPI_I2S_FLAG_BSY) == SET) { }
    GPIO_SetBits(GPIOB, GPIO_Pin_12);                       /* CS̄ = 1，锁存 */

    if (pm) { __enable_irq(); }
}

/* 左右两帧都写完后拉一次 LDAC̄（≥100 ns，8 个 __NOP @64MHz ≈ 125 ns），
   两路输出同一时刻更新，左右零相差 */
static void dac_flush(void)
{
    dac_write(CMD_DACA, g_left);
    dac_write(CMD_DACB, g_right);
    uint32_t pm = __get_PRIMASK();
    __disable_irq();
    GPIO_ResetBits(GPIOB, GPIO_Pin_0);                      /* LDAC̄ = 0 */
    __NOP(); __NOP(); __NOP(); __NOP();
    __NOP(); __NOP(); __NOP(); __NOP();
    GPIO_SetBits(GPIOB, GPIO_Pin_0);                        /* LDAC̄ = 1 */
    if (pm) { __enable_irq(); }
}

/* ------------------------------------------------------------ 串口（轻量，不引 printf） */
static void uart_putc(char c)
{
    USART_SendData(USART1, (uint8_t)c);
    while (USART_GetFlagStatus(USART1, USART_FLAG_TXE) == RESET) { }
}
static void uart_puts(const char *s) { while (*s) { uart_putc(*s++); } }
static void uart_putdec(uint32_t v)
{
    char b[11]; int i = 10;
    b[10] = 0;
    do { b[--i] = (char)('0' + v % 10u); v /= 10u; } while (v);
    uart_puts(&b[i]);
}

/* ------------------------------------------------------------ 时钟：HSI→PLL 64 MHz */
static void clock_init_64mhz(void)
{
    /* HSI 8MHz ÷2 ×16 = 64 MHz：完全不依赖板上晶振 */
    RCC_PLLConfig(RCC_PLLSource_HSI_Div2, RCC_PLLMul_16);
    RCC_HCLKConfig(RCC_SYSCLK_Div1);        /* AHB  = 64 MHz */
    RCC_PCLK1Config(RCC_HCLK_Div2);         /* APB1 = 32 MHz（SPI2/TIM2 在此） */
    RCC_PCLK2Config(RCC_HCLK_Div1);         /* APB2 = 64 MHz（SPI1/USART1 在此） */
    RCC_PLLCmd(ENABLE);
    while (RCC_GetFlagStatus(RCC_FLAG_PLLRDY) == RESET) { }
    RCC_SYSCLKConfig(RCC_SYSCLKSource_PLLCLK);
    while (RCC_GetSYSCLKSource() != 0x08) { }               /* 0x08 = PLL */
    SystemCoreClockUpdate();                                /* 64 MHz */
}

/* ------------------------------------------------------------ GPIO */
static void gpio_init(void)
{
    GPIO_InitTypeDef io;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_AFIO | RCC_APB2Periph_GPIOA |
                           RCC_APB2Periph_GPIOB, ENABLE);

    /* PA4=SPI1_NSS 输入浮空  PA5=SPI1_SCK 输入浮空  PA7=SPI1_MOSI 输入浮空 */
    io.GPIO_Pin  = GPIO_Pin_4 | GPIO_Pin_5 | GPIO_Pin_7;
    io.GPIO_Mode = GPIO_Mode_IN_FLOATING;
    io.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOA, &io);

    /* PA9=USART1_TX 复用推挽 */
    io.GPIO_Pin  = GPIO_Pin_9;
    io.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOA, &io);

    /* PB0=LDAC̄ 推挽输出，空闲为高 */
    io.GPIO_Pin  = GPIO_Pin_0;
    io.GPIO_Mode = GPIO_Mode_Out_PP;
    GPIO_Init(GPIOB, &io);
    GPIO_SetBits(GPIOB, GPIO_Pin_0);

    /* PB12=CS̄ 推挽输出，空闲为高 */
    io.GPIO_Pin  = GPIO_Pin_12;
    GPIO_Init(GPIOB, &io);
    GPIO_SetBits(GPIOB, GPIO_Pin_12);

    /* PB13=SPI2_SCK 复用推挽（主机） */
    io.GPIO_Pin  = GPIO_Pin_13;
    io.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOB, &io);

    /* PB15=SPI2_MOSI 复用推挽（主机） */
    io.GPIO_Pin  = GPIO_Pin_15;
    GPIO_Init(GPIOB, &io);

    /* PC13 = 板载 LED（低有效），状态指示用 */
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOC, ENABLE);
    io.GPIO_Pin  = GPIO_Pin_13;
    io.GPIO_Mode = GPIO_Mode_Out_PP;
    io.GPIO_Speed = GPIO_Speed_2MHz;
    GPIO_Init(GPIOC, &io);
    GPIO_ResetBits(GPIOC, GPIO_Pin_13);     /* 上电先点亮，证明程序在跑 */
}

/* ------------------------------------------------------------ SPI1 从机（收 FPGA） */
static void spi1_slave_init(void)   /* 16bit、模式 0,0、硬件 NSS */
{
    SPI_InitTypeDef si;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_SPI1, ENABLE);
    SPI_I2S_DeInit(SPI1);
    si.SPI_Direction         = SPI_Direction_2Lines_FullDuplex;
    si.SPI_Mode              = SPI_Mode_Slave;
    si.SPI_DataSize          = SPI_DataSize_16b;
    si.SPI_CPOL              = SPI_CPOL_Low;        /* 模式 0,0 */
    si.SPI_CPHA              = SPI_CPHA_1Edge;      /* 首沿采样 */
    si.SPI_NSS               = SPI_NSS_Hard;        /* PA4 = FPGA 的 CS̄ */
    si.SPI_BaudRatePrescaler = SPI_BaudRatePrescaler_2;   /* 从机无意义 */
    si.SPI_FirstBit          = SPI_FirstBit_MSB;
    si.SPI_CRCPolynomial     = 7;
    SPI_Init(SPI1, &si);

    SPI_I2S_ITConfig(SPI1, SPI_I2S_IT_RXNE, ENABLE);
    SPI_Cmd(SPI1, ENABLE);
}

/* ------------------------------------------------------------ SPI2 主机（发 MCP4922） */
static void spi2_master_init(void)  /* 16bit、模式 0,0、4 MHz */
{
    SPI_InitTypeDef si;

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_SPI2, ENABLE);
    si.SPI_Direction         = SPI_Direction_2Lines_FullDuplex;
    si.SPI_Mode              = SPI_Mode_Master;
    si.SPI_DataSize          = SPI_DataSize_16b;
    si.SPI_CPOL              = SPI_CPOL_Low;
    si.SPI_CPHA              = SPI_CPHA_1Edge;
    si.SPI_NSS               = SPI_NSS_Soft;        /* NSS 由 PB12 手拉 */
    si.SPI_BaudRatePrescaler = SPI_BaudRatePrescaler_8;   /* 32/8 = 4 MHz */
    si.SPI_FirstBit          = SPI_FirstBit_MSB;
    si.SPI_CRCPolynomial     = 7;
    SPI_Init(SPI2, &si);
    SPI_Cmd(SPI2, ENABLE);
}

/* ------------------------------------------------------------ EXTI4（PA4 下降沿） */
static void exti4_init(void)        /* 字边界 = 左声道起点 */
{
    EXTI_InitTypeDef ei;
    NVIC_InitTypeDef ni;

    GPIO_EXTILineConfig(GPIO_PortSourceGPIOA, GPIO_PinSource4);

    ei.EXTI_Line    = EXTI_Line4;
    ei.EXTI_Mode    = EXTI_Mode_Interrupt;
    ei.EXTI_Trigger = EXTI_Trigger_Falling;
    ei.EXTI_LineCmd = ENABLE;
    EXTI_Init(&ei);

    ni.NVIC_IRQChannel                   = EXTI4_IRQn;
    ni.NVIC_IRQChannelPreemptionPriority = 1;
    ni.NVIC_IRQChannelSubPriority        = 0;
    ni.NVIC_IRQChannelCmd                = ENABLE;
    NVIC_Init(&ni);
}

/* ------------------------------------------------------------ TIM2 自检音 24.42 kHz */
static void tim2_testtone_init(void)    /* 64MHz/2621 ≈ 24.42 kHz，≈fs */
{
    TIM_TimeBaseInitTypeDef ti;
    NVIC_InitTypeDef ni;

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2, ENABLE);
    ti.TIM_Prescaler     = 0;
    ti.TIM_Period        = 2620;
    ti.TIM_ClockDivision = TIM_CKD_DIV1;
    ti.TIM_CounterMode   = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM2, &ti);            /* 内部已产生 UG 重装影子寄存器 */
    TIM_ClearFlag(TIM2, TIM_FLAG_Update);
    TIM_ITConfig(TIM2, TIM_IT_Update, ENABLE);

    ni.NVIC_IRQChannel                   = TIM2_IRQn;
    ni.NVIC_IRQChannelPreemptionPriority = 2;
    ni.NVIC_IRQChannelSubPriority        = 0;
    ni.NVIC_IRQChannelCmd                = ENABLE;
    NVIC_Init(&ni);

    TIM_Cmd(TIM2, ENABLE);
}

/* ------------------------------------------------------------ SysTick 1 ms */
static void systick_init(void)
{
    SysTick_Config(SystemCoreClock / 1000u);    /* 64 MHz / 1000 */
}

/* ------------------------------------------------------------ USART1 状态口 */
static void usart1_init(void)       /* PA9 115200-8-N-1 */
{
    USART_InitTypeDef ui;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1, ENABLE);
    ui.USART_BaudRate            = 115200;
    ui.USART_WordLength          = USART_WordLength_8b;
    ui.USART_StopBits            = USART_StopBits_1;
    ui.USART_Parity              = USART_Parity_No;
    ui.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    ui.USART_Mode                = USART_Mode_Tx;
    USART_Init(USART1, &ui);
    USART_Cmd(USART1, ENABLE);
}

/* ------------------------------------------------------------ 中断服务 */
void EXTI4_IRQHandler(void)         /* FPGA CS̄ 下降沿：本字是左声道 */
{
    if (EXTI_GetITStatus(EXTI_Line4) != RESET) {
        EXTI_ClearITPendingBit(EXTI_Line4);
        g_side = 0;
    }
}

void SPI1_IRQHandler(void)          /* 每个字一次：16bit 已在 DR 里 */
{
    if (SPI_I2S_GetFlagStatus(SPI1, SPI_I2S_FLAG_OVR) != RESET) {
        /* 上一个字没取走就被覆盖。清 OVR 序列：读 SR 再读 DR */
        g_ovr++;
        (void)SPI_I2S_GetFlagStatus(SPI1, SPI_I2S_FLAG_OVR);
        (void)SPI_I2S_ReceiveData(SPI1);
        return;
    }
    uint16_t w = SPI_I2S_ReceiveData(SPI1);
    g_last_word_ms = g_ms;
    if (g_side == 0u) {             /* 左声道字 */
        g_side = 1u;
        g_left = w;
    } else {                        /* 右声道字：凑齐一对，写 DAC */
        g_side = 0u;
        g_right = w;
        dac_flush();
    }
    g_words++;
}

void TIM2_IRQHandler(void)          /* ≈24.42 kHz：无 FPGA 时发自检音 */
{
    if (TIM_GetITStatus(TIM2, TIM_IT_Update) != RESET) {
        TIM_ClearITPendingBit(TIM2, TIM_IT_Update);
        if (g_test_mode) {
            g_phase = (uint16_t)(g_phase + 1180u);  /* 65536×440/24418 ≈ 440 Hz */
            uint16_t s = g_sine256[(g_phase >> 8) & 0xFFu];
            g_left = s; g_right = s;
            dac_flush();
        }
    }
}

/* ============================================================ main */
int main(void)
{
    clock_init_64mhz();
    gpio_init();
    usart1_init();
    spi2_master_init();
    spi1_slave_init();
    exti4_init();
    tim2_testtone_init();
    systick_init();

    /* 开局先写静音中值，DAC 输出 1.65 V，喇叭安静 */
    g_left = g_right = 2048u;
    dac_flush();
    g_test_mode = 1u;               /* 上电即自检音，收到 FPGA 字自动退出 */

    uart_puts("\r\nF103 SPI-MCP4922 bridge v1.1 (StdPeriph, Keil5)\r\n");
    uart_puts("UART PA9 115200. Test tone until FPGA data arrives.\r\n");

    uint32_t last_report = 0, last_words = 0, last_led = 0;
    for (;;) {
        uint32_t now = g_ms;
        /* 无 FPGA 超过 1 s → 自检音；有数据 → 直通。切模式无 racing：
           dac_write/dac_flush 内部已原子化 */
        uint8_t want_test = ((now - g_last_word_ms) > 1000u) ? 1u : 0u;
        if (want_test != g_test_mode) {
            __disable_irq();
            g_test_mode = want_test;
            if (!want_test) { g_phase = 0; }
            __enable_irq();
        }

        /* LED 状态：TEST=1 Hz 慢闪；PASS=常亮（低有效） */
        if (g_test_mode) {
            if (now - last_led >= 500u) {
                last_led = now;
                GPIO_WriteBit(GPIOC, GPIO_Pin_13,
                    (BitAction)(1 - GPIO_ReadOutputDataBit(GPIOC, GPIO_Pin_13)));
            }
        } else {
            last_led = now;
            GPIO_ResetBits(GPIOC, GPIO_Pin_13);     /* 亮 */
        }

        if (now - last_report >= 500u) {
            last_report = now;
            uint32_t dw = g_words - last_words;
            last_words = g_words;
            uart_puts("mode=");
            uart_puts(g_test_mode ? "TEST(440Hz)" : "PASS");
            uart_puts(" words/s=");
            uart_putdec(dw * 2u);       /* 500ms 窗口 ×2 */
            uart_puts(" fs=");
            uart_putdec(dw);            /* 每秒声道对数 ≈ 采样率 */
            uart_puts(" ovr=");
            uart_putdec(g_ovr);
            uart_puts("\r\n");
        }
    }
}
