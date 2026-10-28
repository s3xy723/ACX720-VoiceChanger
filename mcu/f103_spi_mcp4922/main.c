/* ============================================================================
 *  main.c —— STM32F103C8T6：FPGA→MCP4922 全 SPI 音频桥（无 I2S 方案）
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
 *  时钟：HSI 8MHz → PLL×16(÷2) = 64 MHz（不依赖板上晶振，任何 C8T6 板都能跑）
 *  串口：PA9(TX) 115200-8-N-1，每 500 ms 打一行状态。
 *
 *  编译（见 build.sh）：arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb ...
 *  烧录：ST-Link Utility / STM32CubeProgrammer 直接烧 firmware.hex
 * ==========================================================================*/
#include <stdint.h>
#include "oled.h"
#include "cfg_proto.h"

/* ------------------------------------------------------------ 寄存器地址 */
#define REG32(a)        (*(volatile uint32_t *)(a))

#define RCC_BASE        0x40021000u
#define RCC_CR          REG32(RCC_BASE + 0x00)
#define RCC_CFGR        REG32(RCC_BASE + 0x04)
#define RCC_APB2ENR     REG32(RCC_BASE + 0x18)
#define RCC_APB1ENR     REG32(RCC_BASE + 0x1C)
#define FLASH_ACR       REG32(0x40022000u)   /* FLASH 等待周期/预取 */

#define GPIOA_BASE      0x40010800u
#define GPIOA_CRL       REG32(GPIOA_BASE + 0x00)
#define GPIOA_CRH       REG32(GPIOA_BASE + 0x04)
#define GPIOA_BSRR      REG32(GPIOA_BASE + 0x10)

#define GPIOB_BASE      0x40010C00u
#define GPIOB_CRL       REG32(GPIOB_BASE + 0x00)
#define GPIOB_CRH       REG32(GPIOB_BASE + 0x04)
#define GPIOB_BSRR      REG32(GPIOB_BASE + 0x10)

#define AFIO_EXTICR2    REG32(0x40010000u + 0x0C)   /* EXTI4~7 端口选择 */

#define EXTI_IMR        REG32(0x40010400u + 0x00)
#define EXTI_FTSR       REG32(0x40010400u + 0x08)
#define EXTI_PR         REG32(0x40010400u + 0x14)

#define SPI1_CR1        REG32(0x40013000u + 0x00)
#define SPI1_CR2        REG32(0x40013000u + 0x04)
#define SPI1_SR         REG32(0x40013000u + 0x08)
#define SPI1_DR         REG32(0x40013000u + 0x0C)

#define SPI2_CR1        REG32(0x40003800u + 0x00)
#define SPI2_SR         REG32(0x40003800u + 0x08)
#define SPI2_DR         REG32(0x40003800u + 0x0C)

#define USART1_SR       REG32(0x40013800u + 0x00)
#define USART1_DR       REG32(0x40013800u + 0x04)
#define USART1_BRR      REG32(0x40013800u + 0x08)
#define USART1_CR1      REG32(0x40013800u + 0x0C)

#define TIM2_CR1        REG32(0x40000000u + 0x00)
#define TIM2_DIER       REG32(0x40000000u + 0x0C)
#define TIM2_SR         REG32(0x40000000u + 0x10)
#define TIM2_EGR        REG32(0x40000000u + 0x14)
#define TIM2_PSC        REG32(0x40000000u + 0x28)
#define TIM2_ARR        REG32(0x40000000u + 0x2C)

#define NVIC_ISER0      REG32(0xE000E100u)
#define NVIC_ISER1      REG32(0xE000E104u)
#define NVIC_IPR        ((volatile uint8_t *)0xE000E400u)

/* ---- USART2 接收（听上位机发来的"工作模式"配置包）----------------------
 * 为什么用 USART2 而不是 USART1：USART1(PA9) 是调试口，一直按 115200 打
 * 状态行；配置流是 1.5 Mbaud，同一个 USART 只能有一个 BRR —— 挪到 USART2
 * 两个用途互不干扰。PA3 = USART2_RX，只收不发。
 * ---------------------------------------------------------------------- */
#define USART2_SR       REG32(0x40004400u + 0x00)
#define USART2_DR       REG32(0x40004400u + 0x04)
#define USART2_BRR      REG32(0x40004400u + 0x08)
#define USART2_CR1      REG32(0x40004400u + 0x0C)
#define USART2_CR3      REG32(0x40004400u + 0x14)
#define DMA1_CCR6       REG32(0x40020000u + 0x74)
#define DMA1_CNDTR6     REG32(0x40020000u + 0x78)
#define DMA1_CPAR6      REG32(0x40020000u + 0x7C)
#define DMA1_CMAR6      REG32(0x40020000u + 0x80)
#define RCC_AHBENR      REG32(0x40021000u + 0x14)

#define SYST_CSR        REG32(0xE000E010u)
#define SYST_RVR        REG32(0xE000E014u)
#define SYST_CVR        REG32(0xE000E018u)

/* ------------------------------------------------------------ SPI 位定义 */
#define SPI_CR1_SPE     (1u << 6)
#define SPI_CR1_MSTR    (1u << 2)
#define SPI_CR1_CPOL    (1u << 1)
#define SPI_CR1_CPHA    (1u << 0)
#define SPI_CR1_DFF     (1u << 11)      /* 16 位帧（F1） */
#define SPI_CR1_SSM     (1u << 9)
#define SPI_CR1_SSI     (1u << 8)
#define SPI_CR1_BR_1    (1u << 4)       /* fPCLK/8 */

#define SPI_CR2_RXNEIE  (1u << 6)
#define SPI_SR_RXNE     (1u << 0)
#define SPI_SR_TXE      (1u << 1)
#define SPI_SR_OVR      (1u << 6)
#define SPI_SR_BSY      (1u << 7)

/* ------------------------------------------------------------ GPIO 位定义 */
#define BSRR_SET(pin)   (1u << (pin))
#define BSRR_CLR(pin)   (1u << ((pin) + 16))

/* ------------------------------------------------------------ 全局状态 */
/* MCP4922 命令字：bit15=A/B  bit14=BUF  bit13=GA  bit12=SHDN  bit11..0=数据
 *
 * ★ BUF 位（bit14）必须保持 0 —— 2026-10-22 实测记录，别再改它：
 *   我一度以为 BUF 是"输出缓冲"，把它改成 1 想改善带载能力，结果
 *   **喇叭彻底不出声**（门控 ON/OFF 从 +37 dB 掉到 -5 dB）。
 *   查证后确认：MCP4922 的 bit14 是 **VREF 输入**缓冲控制位，不是输出缓冲；
 *   输出运放本来就是常开的。本板 VREF 直接接 VDD（低阻抗），
 *   开这个缓冲只会改变基准通路，实测就是没输出。
 *   结论：BUF=0 / GA=1(1x) / SHDN=1 是本板的正确组合。
 */
#define CMD_DACA        0x3000u         /* A/B=0, BUF=0, GA=1(1x), SHDN=1 */
#define CMD_DACB        0xB000u         /* A/B=1, BUF=0, GA=1(1x), SHDN=1 */

static volatile uint32_t g_ms;          /* SysTick 毫秒计数 */
static volatile uint32_t g_last_word_ms;/* 最近一次收到 FPGA 字的时刻 */
static volatile uint32_t g_words;       /* 累计收到的 FPGA 字数 */
static volatile uint32_t g_ovr;         /* SPI1 溢出次数（非 0 = 链路跟不上） */
static volatile uint16_t g_left, g_right;
static volatile uint8_t  g_side;        /* 0=下一个字是左声道 1=右声道 */
static volatile uint8_t  g_test_mode;   /* 1 = 无 FPGA 信号，发自检音 */

/* ★ 诊断用：把 SPI1 收到的样值原样存进 RAM 环形缓冲，用调试器读出来看
   波形。只为了回答一个问题：**送到 DAC 的码流本身是不是干净的正弦**。
   如果这里就是台阶状/乱跳，问题在 FPGA→FIFO→SPI 这段（数字域）；
   如果这里是干净正弦而喇叭里是噪声，问题在 DAC 之后的模拟段。
   注意 g_cap_wr 是"写指针"，读出来时用它做环形重排。
   编译加 -DCAPTURE 才生效，正式固件里不存在（不占 RAM、不进 ISR）。 */
#ifdef CAPTURE
#define CAP_N 1024u
static volatile uint16_t g_cap[CAP_N * 2u];
static volatile uint32_t g_cap_wr;
static volatile uint32_t g_edge;        /* EXTI4 次数（正常应 == 帧率 24414/s） */
static volatile uint32_t g_left_n;      /* 被判成"左声道"的字数 */
#endif

#define TEST_AMP_X8     2800            /* 自检音幅度（×1/8 dB 步进余量，见下） */

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

/* ------------------------------------------------------------ 内联中断开关 */
static inline uint32_t primask_save(void)
{
    uint32_t pm;
    __asm volatile ("mrs %0, primask" : "=r" (pm));
    return pm;
}
static inline void irq_disable(void) { __asm volatile ("cpsid i" ::: "memory"); }
static inline void irq_enable(void)  { __asm volatile ("cpsie i" ::: "memory"); }

/* ------------------------------------------------------------ 样值→DAC 码 */
/* ★ 2026-10-22 修：FPGA 送来的 16 位字是**有符号**补码（-32768..+32767），
   MCP4922 要的是 0..4095 的**无符号**码。原来直接 `& 0x0FFF` 是取模而不是
   截断 —— 负半周会翻到接近满量程，输出变成折叠锯齿波：

       实测 440 Hz 正弦输入
         现状 &0xFFF : 相邻样本最大跳变 3223/4095，主峰跑到 8134 Hz，
                       失真+噪声比主音还高 9.1 dB  → 连续咔哒 + 电流声
         正确 >>4    : 最大跳变 186，主峰 440 Hz，失真 0.0 dB

   这就是"声音从音响出来全是爆音、几乎听不清内容"的根因。
   正确做法：先抬到无符号（+32768），再右移 4 位取高 12 位。 */
static uint16_t sample_to_dac(uint16_t w)
{
    int32_t v = (int32_t)(int16_t)w;        /* 符号扩展回 -32768..32767 */
    v += 32768;                             /* 抬到 0..65535（0 V..3.3 V） */
    /* ★ 2026-10-22 再改：这里原来是 `v >>= 4`（直接截断）。
       截断会带来 -0.5 LSB 的系统性偏置，而且量化误差与信号**相关** ——
       不是白噪声，是跟着波形走的失真，小信号时尤其明显（沙沙声）。
       加上半个步长再移 = 四舍五入，量化噪声降一半且与信号解耦。 */
    v = (v + 8) >> 4;                       /* 16 bit -> 12 bit，四舍五入 */
#ifndef OUT_ATTEN_SHIFT
#define OUT_ATTEN_SHIFT 0
#endif
#if OUT_ATTEN_SHIFT > 0
    /* 以中点 2048 为轴衰减（静音电平保持 1.65 V 不动）：
       后级是小喇叭 + D 类功放，实测驱动越大非线性越重。
       0 = 不衰减，1 = -6 dB，2 = -12 dB。 */
    v = 2048 + ((v - 2048) >> OUT_ATTEN_SHIFT);
#endif
    if (v < 0)     { v = 0; }
    if (v > 4095)  { v = 4095; }
    return (uint16_t)v;
}

/* ------------------------------------------------------------ MCP4922 写入 */
/* 原子保护：SPI1 RXNE 与 TIM2（自检音）都会调它，两处 ISR 抢 SPI2 会把
   命令帧拆散，所以整个"CS̄低→16bit→CS̄高"序列必须关中断完成（约 4 µs）。 */
static void dac_write(uint16_t cmd, uint16_t d12)
{
    uint32_t pm = primask_save();
    irq_disable();

    while (SPI2_SR & SPI_SR_BSY) { }
    GPIOB_BSRR = BSRR_CLR(12);              /* CS̄ = 0（PB12） */
    SPI2_DR = (uint32_t)(cmd | (d12 & 0x0FFFu));
    while (!(SPI2_SR & SPI_SR_TXE)) { }
    while (SPI2_SR & SPI_SR_BSY) { }
    GPIOB_BSRR = BSRR_SET(12);              /* CS̄ = 1，锁入输入寄存器 */

    if (!pm) { irq_enable(); }
}

/* 左右两帧都写完后拉一次 LDAC̄（≥100 ns，8 个 NOP @64MHz ≈ 125 ns），
   两路输出同一时刻更新，左右零相差 */
static void dac_flush(void)
{
    dac_write(CMD_DACA, sample_to_dac(g_left));
    dac_write(CMD_DACB, sample_to_dac(g_right));
    uint32_t pm = primask_save();
    irq_disable();
    GPIOB_BSRR = BSRR_CLR(0);               /* PB0 = LDAC̄ 0 */
    __asm volatile ("nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop" ::: "memory");
    GPIOB_BSRR = BSRR_SET(0);               /* PB0 = LDAC̄ 1 */
    if (!pm) { irq_enable(); }
}

/* ------------------------------------------------------------ 串口（无库） */
static void uart_putc(char c)
{
    while (!(USART1_SR & (1u << 7))) { }    /* TXE */
    USART1_DR = (uint8_t)c;
}
static void uart_puts(const char *s) { while (*s) { uart_putc(*s++); } }
static void uart_putdec(uint32_t v)
{
    char b[11]; int i = 10;
    b[10] = 0;
    do { b[--i] = (char)('0' + v % 10u); v /= 10u; } while (v);
    uart_puts(&b[i]);
}

/* ==================================================================
 *  上位机 → MCU 的「工作模式」配置包（给 OLED 用）
 *
 *  物理链路：CH340 的 TXD 一根线同时接 FPGA 的 uart_rx 和 STM32 的 PA3
 *  (USART2_RX)。这根线本来就在传 1.5 Mbaud 的音频帧，配置包只是夹在帧之间
 *  的一小段独立突发（AA 55 …）。FPGA 的状态机在找 A5 5A，会跳过它。
 *
 *  协议本身（帧格式 / CRC / 字段含义）全在 cfg_proto.h 里写，这里只管
 *  "收字节、存结果、给界面用"。
 * ================================================================== */
#define RX_BUF_SZ       512

static volatile cfg_t g_cfg;
static volatile uint32_t g_cfg_ok, g_cfg_bad, g_cfg_byte;
static uint8_t rx_buf[RX_BUF_SZ];
static uint32_t rx_scan;                 /* 已解析到的位置（环形） */

static void cfg_feed(uint8_t b)
{
    cfg_t c;
    if (cfg_parse_byte(b, &c)) {
        c.stamp = g_ms;
        g_cfg = c;
        g_cfg_ok++;
    }
}

/* 把 DMA 里新到的字节喂进状态机。cur 是 DMA 剩余计数换算出的写位置 */
static void cfg_pump(void)
{
    uint32_t cur = RX_BUF_SZ - DMA1_CNDTR6;
    if (cur > RX_BUF_SZ) { cur = 0; }
    while (rx_scan != cur) {
        cfg_feed(rx_buf[rx_scan]);
        g_cfg_byte++;
        rx_scan++;
        if (rx_scan >= RX_BUF_SZ) { rx_scan = 0; }
    }
}

static void clock_init_64mhz(void)
{
    /* HSI 8MHz ÷2 ×16 = 64 MHz：完全不依赖板上晶振 */
    RCC_CFGR = (RCC_CFGR & ~(0xFu << 18)) | (0xEu << 18);  /* PLLMUL ×16 */
    /* PLLSRC=0（HSI/2），PPRE1 默认 0（APB1=HCLK/2? 不对：默认 PCLK1=HCLK/2
       需要设 PPRE1=100）。设 PPRE1 = 100b（/2）→ APB1 = 32 MHz */
    RCC_CFGR = (RCC_CFGR & ~(0x7u << 8)) | (0x4u << 8);
    RCC_CR  |= (1u << 24);                                  /* PLLON */
    while (!(RCC_CR & (1u << 25))) { }                      /* PLLRDY */
    /* ★ 64 MHz 必须先把 FLASH 等待周期设成 2，否则切到 PLL 后取指
       会读到错误指令 → HardFault（实测 PC 跳到 0x4xxxxxxx 乱码）。
       这一步必须在 SW=PLL 之前做。 */
    FLASH_ACR = (FLASH_ACR & ~0x7u) | 0x2u;                 /* 2 wait states */
    RCC_CFGR = (RCC_CFGR & ~3u) | 2u;                       /* SW = PLL */
    while ((RCC_CFGR & (3u << 2)) != (2u << 2)) { }         /* SWS = PLL */
}

static void gpio_init(void)
{
    RCC_APB2ENR |= (1u << 0)   /* AFIOEN */
                 | (1u << 2)   /* IOPAEN */
                 | (1u << 3)   /* IOPBEN */
                 | (1u << 12)  /* SPI1EN */
                 | (1u << 14); /* USART1EN */
    RCC_APB1ENR |= (1u << 0)   /* TIM2EN */
                 | (1u << 14); /* SPI2EN */

    /* PA4=SPI1_NSS 输入浮空(0x4)  PA5=SPI1_SCK 输入浮空  PA7=SPI1_MOSI 输入浮空 */
    GPIOA_CRL = 0x44444444u;
    /* PA9=USART1_TX 复用推挽 50MHz(0xB)，其余浮空 */
    GPIOA_CRH = 0x444444B4u;
    /* PB0=LDAC̄ 推挽输出(0x3)，PB1~7 浮空 */
    GPIOB_CRL = 0x44444443u;
    /* PB12=CS̄ 推挽输出(0x3)  PB13=SPI2_SCK 复用推挽(0xB)  PB15=SPI2_MOSI 复用推挽(0xB)
     * ★ 修正（2026-10-22）：原值 0xB4344444u 把 PB13 配成 0x3(普通推挽输出)、
     *   PB12 配成 0x4(浮空输入) —— SCK 被软件 GPIO 锁在低电平、CS̄ 根本没被驱动，
     *   MCP4922 永远收不到片选和时钟 → 完全无声。正确应为 PB13=0xB、PB12=0x3。 */
    GPIOB_CRH = 0xB4B34444u;

    GPIOB_BSRR = BSRR_SET(12) | BSRR_SET(0);  /* CS̄ / LDAC̄ 空闲为高 */
}

static void spi1_slave_init(void)   /* 收 FPGA：16bit、模式 0,0、硬件 NSS */
{
    SPI1_CR1 = SPI_CR1_DFF;             /* 从机：MSTR=0，CPOL=0，CPHA=0，SSM=0 */
    SPI1_CR2 = SPI_CR2_RXNEIE;
    SPI1_CR1 |= SPI_CR1_SPE;
}

static void spi2_master_init(void)  /* 发 MCP4922：16bit、模式 0,0、4 MHz */
{
    SPI2_CR1 = SPI_CR1_MSTR | SPI_CR1_BR_1      /* fPCLK1(32MHz)/8 = 4 MHz */
             | SPI_CR1_SSM | SPI_CR1_SSI        /* NSS 由 PB12 手拉 */
             | SPI_CR1_DFF;
    SPI2_CR1 |= SPI_CR1_SPE;
}

static void exti4_init(void)        /* PA4 下降沿 = 字边界 = 左声道起点 */
{
    AFIO_EXTICR2 &= ~0xFu;              /* EXTI4 ← PAx（0000 = port A） */
    EXTI_FTSR |= (1u << 4);
    EXTI_IMR  |= (1u << 4);
}

static void tim2_testtone_init(void) /* 64MHz/2621 ≈ 24.42 kHz，≈fs */
{
    TIM2_PSC = 0;
    TIM2_ARR = 2620;
    TIM2_DIER = (1u << 0);              /* UIE */
    TIM2_EGR = 1u;                      /* 重装 PSC/ARR */
    TIM2_CR1 = 1u;                      /* CEN */
}

static void systick_init(void)      /* 1 ms 节拍 */
{
    SYST_RVR = 63999u;                  /* 64 MHz / 1000 - 1 */
    SYST_CVR = 0;
    SYST_CSR = 0x7;                     /* ENABLE | TICKINT | CLKSOURCE=CPU */
}

static void usart1_init(void)
{
    /* PCLK2 = 64 MHz，115200：USARTDIV = 64e6/(16×115200) = 34.72
       → BRR = 34<<4 | 11 = 0x22B，实测误差 +0.08% */
    USART1_BRR = 0x22B;
    USART1_CR1 = (1u << 13) | (1u << 3);    /* UE | TE */
}

/* USART2 只收：1.5 Mbaud、DMA 环形缓冲、靠 IDLE 中断收口。
   波特率算账（PCLK1 = 32 MHz）：USARTDIV = 32e6/(16×1.5e6) = 1.333
   → BRR = 1<<4 | 5 = 0x15 → 32e6/(16×1.3125) = 1.5238 Mbaud（+1.6%）。
   8N1 整帧 10 bit，末位累计偏差 16% 个位宽，在容差内（实测能收）。
   ★ 为什么用 DMA + IDLE 而不是每字节中断：这条线上 99 kB/s 的音频帧
   在跑，逐字节中断会吃掉 10 万次/秒的 CPU —— 直接毁掉音频。DMA 搬完
   只在每帧末尾（IDLE）进一次中断，约 380 次/秒。 */
static void usart2_rx_init(void)
{
    RCC_APB1ENR |= (1u << 17);              /* USART2EN */
    RCC_AHBENR  |= (1u << 0);               /* DMA1EN */

    USART2_BRR = 0x15;
    USART2_CR3 |= (1u << 6);                /* DMAR */
    USART2_CR1 |= (1u << 13) | (1u << 2) | (1u << 4);  /* UE | RE | IDLEIE */

    DMA1_CCR6 = 0;
    while (DMA1_CCR6 & 1u) { }
    DMA1_CPAR6   = (uint32_t)&USART2_DR;
    DMA1_CMAR6   = (uint32_t)rx_buf;
    DMA1_CNDTR6  = RX_BUF_SZ;
    DMA1_CCR6    = (1u << 0) | (1u << 7) | (1u << 10);  /* EN | MINC | CIRC */
}

/* ------------------------------------------------------------ 中断服务 */
void EXTI4_IRQHandler(void)          /* FPGA CS̄ 下降沿：本字是左声道 */
{
    EXTI_PR = (1u << 4);             /* 写 1 清挂起 */
    g_side = 0;
#ifdef CAPTURE
    g_edge++;
#endif
}

void SPI1_IRQHandler(void)           /* 每个字一次：16bit 已在 DR 里 */
{
    if (SPI1_SR & SPI_SR_OVR) {      /* 上一个字没取走就被覆盖 */
        g_ovr++;
        /* ★ 2026-10-23 修：清 OVR 的**顺序**必须"先读 DR、再读 SR"
         *   （RM0008：OVR is cleared by a read to SPI_DR followed by a read
         *   to SPI_SR）。原来是先读 SR 再读 DR，OVR 清不掉 —— 中断线一直有效，
         *   处理器退出 ISR 立刻又进来，每 20 µs 加一次 g_ovr，
         *   观感就是"g_words 冻住不动、g_ovr 每秒涨 4.9 万"，
         *   整个固件卡死在 OVR 分支里，音频彻底断掉。
         *   只要用调试器 halt 一秒（调试时必然发生）就会踩到，所以这是
         *   "读一次计数器后声音就没了"的真实原因，不是 DAC/模拟段的问题。 */
        (void)SPI1_DR;               /* 先读 DR */
        (void)SPI1_SR;               /* 再读 SR：OVR 这时才真正清掉 */
        g_side = 0u;                 /* 丢字后重新对齐帧边界，避免左右错位 */
        return;
    }
    uint16_t w = (uint16_t)SPI1_DR;
    g_last_word_ms = g_ms;
    if (g_side == 0u) {              /* 左声道字 */
        g_side = 1u;
        g_left = w;
#ifdef CAPTURE
        g_left_n++;
#endif
    } else {                         /* 右声道字：凑齐一对，写 DAC */
        g_side = 0u;
        g_right = w;
#ifdef CAPTURE
        {   /* 诊断：把这一对原样存起来（环形），别的什么都不影响 */
            uint32_t k = (g_cap_wr & (CAP_N - 1u)) * 2u;
            g_cap[k]      = g_left;
            g_cap[k + 1u] = g_right;
            g_cap_wr++;
        }
#endif
        dac_flush();
    }
    g_words++;
}

/* USART2：靠 IDLE 收口。读 SR + 读 DR 清 IDLE/ORE，然后把 DMA 里新到的
   字节喂给配置状态机。整个过程几微秒，不会影响音频 ISR。 */
void USART2_IRQHandler(void)
{
    uint32_t sr = USART2_SR;
    if (sr & ((1u << 4) | (1u << 3) | (1u << 5))) {   /* IDLE | ORE | RXNE */
        (void)USART2_DR;
        cfg_pump();
    }
}

static uint16_t g_phase;             /* 自检音相位累加器（16bit） */
void TIM2_IRQHandler(void)           /* ≈24.42 kHz：无 FPGA 时发自检音 */
{
    TIM2_SR = 0;                     /* 清 UIF */
    if (!g_test_mode) { return; }
    g_phase = (uint16_t)(g_phase + 1180u);   /* 65536×440/24418 ≈ 440 Hz */
    /* ★ g_sine256 存的是 **12 位 DAC 码**（中心 2048、幅度 1400），不是有符号
       16 位样值。直接塞给 sample_to_dac() 会被当成 -32768..32767 再 +32768，
       结果是"叠在 2176 码直流上的 ±90 码小纹波" —— 自检音几乎听不见，
       还带直流偏移。必须先把 DAC 码换算回有符号样值。 */
    uint16_t code = g_sine256[(g_phase >> 8) & 0xFFu];
    int16_t s = (int16_t)(((int32_t)code - 2048) * 16);
    g_left = (uint16_t)s; g_right = (uint16_t)s;
    dac_flush();
}

void SysTick_Handler(void)           /* 1 ms */
{
    g_ms++;
}

/* ------------------------------------------------------------ OLED 显示 */
static char *s_app(char *p, const char *t)
{
    while (*t) { *p++ = *t++; }
    *p = '\0';
    return p;
}

static char *u_app(char *p, uint32_t v, int minw)
{
    char t[12];
    int n = 0;
    do { t[n++] = (char)('0' + (v % 10u)); v /= 10u; } while (v);
    while (n < minw) { t[n++] = '0'; }
    while (n) { *p++ = t[--n]; }
    *p = '\0';
    return p;
}

static char *i_app(char *p, int32_t v)
{
    if (v < 0) { *p++ = '-'; v = -v; }
    else       { *p++ = '+'; }
    return u_app(p, (uint32_t)v, 1);
}

/* 定点：v=1297,dec=2 -> "12.97"；v=46,dec=1 -> "4.6" */
static char *f_app(char *p, uint32_t v, int dec)
{
    uint32_t d = 1;
    int i;
    for (i = 0; i < dec; i++) { d *= 10u; }
    p = u_app(p, v / d, 1);
    *p++ = '.';
    *p = '\0';
    return u_app(p, v % d, dec);
}

/* 内容签名：变了才重画，省得白刷 I2C */
static uint32_t oled_sig(void)
{
    const volatile cfg_t *c = &g_cfg;
    uint8_t link = (uint8_t)((g_cfg_ok != 0u && (g_ms - c->stamp) < 3000u) ? 1u : 0u);
    return (uint32_t)link << 24 |
           (((uint32_t)c->mode & 3u) << 22) |
           (((uint32_t)c->engine & 3u) << 20) |
           (((uint32_t)(uint16_t)c->ratio1000 & 0x7FFu) << 8) |
           ((uint32_t)c->mode << 7) |
           (((uint32_t)(uint16_t)c->semi10 & 0x1FFu) << 3) |
           ((uint32_t)c->vgain ^ (uint32_t)c->again);
}

static void oled_refresh(void)
{
    char b[24];
    char *e;
    const volatile cfg_t *c = &g_cfg;
    uint8_t link = (uint8_t)((g_cfg_ok != 0u && (g_ms - c->stamp) < 3000u) ? 1u : 0u);

    if (!oled_ok()) {
        return;
    }
    oled_clear();

    /* 第 0 页：型号 + 链路状态（LINK = 3 秒内收到过配置包） */
    oled_text(0, 0, "ACX720");
    oled_text(0, 96, link ? "LINK" : "  --");

    if (!link) {
        oled_text(2, 0, "WAIT HOST");
        oled_text(4, 0, "PA3 = USART2");
        oled_text(6, 0, "1.5M cfg");
        oled_flush();
        return;
    }

    if (c->mode == 0u) {
        /* ---- 变调：中文"变调" + 变调比 + 半音 + dphase ---- */
        oled_cjk(2, 0, 0x53D8);            /* 变 */
        oled_cjk(2, 16, 0x8C03);           /* 调 */
        e = b; e = f_app(e, c->ratio1000, 3); s_app(e, "x");
        oled_text(2, 36, b);

        e = b;
        e = s_app(e, (c->engine == 3u) ? "MODEL" : "FPGA");
        e = s_app(e, " SEMI");
        e = i_app(e, (int32_t)c->semi10 / 10);
        e = s_app(e, ".");
        e = u_app(e, (uint32_t)(c->semi10 < 0 ? -(c->semi10 % 10)
                                             : (c->semi10 % 10)), 1);
        oled_text(4, 0, b);

        e = b; e = s_app(e, "DPH"); e = i_app(e, c->dphase);
        e = s_app(e, " G");  e = i_app(e, c->gain_db);
        oled_text(6, 0, b);
    } else {
        /* ---- 分离：中文"分离" + 引擎名 + 人声/伴奏增益 ---- */
        oled_cjk(2, 0, 0x5206);            /* 分 */
        oled_cjk(2, 16, 0x79BB);           /* 离 */
        oled_text(2, 36, (c->engine == 2u) ? "LOCAL" : "CENTER");

        e = b; e = s_app(e, "VOC"); e = u_app(e, c->vgain, 1);
        e = s_app(e, "%  ACC"); e = u_app(e, c->again, 1); e = s_app(e, "%");
        oled_text(4, 0, b);

        e = b; e = s_app(e, "GAIN"); e = i_app(e, c->gain_db);
        e = s_app(e, "dB");
        oled_text(6, 0, b);
    }
    oled_flush();
}

/* ------------------------------------------------------------ main */
int main(void)
{
    clock_init_64mhz();
    gpio_init();
    usart1_init();
    usart2_rx_init();                /* 听上位机的模式配置包（PA3） */
    spi2_master_init();
#ifndef TONE_ONLY
    spi1_slave_init();
#endif
    exti4_init();
    tim2_testtone_init();
    systick_init();

    /* NVIC：EXTI4 是 IRQ10（不是 17！），TIM2 是 IRQ28，SPI1 是 IRQ35，
       USART2 是 IRQ38。优先级：音频三条（EXTI4/TIM2/SPI1）保持 0，
       USART2 设成 0x80 —— 配置包晚几微秒无所谓，音频不能被拖。
       （NVIC_IPR 是"每 IRQ 一个字节"，所以数组下标就是 IRQ 号） */
    NVIC_ISER0 = (1u << 10) | (1u << 28);
#ifndef TONE_ONLY
    NVIC_ISER1 = (1u << 3);
#endif
    NVIC_IPR[38] = 0x80;
    NVIC_ISER1 |= (1u << 6);         /* USART2 = IRQ38 -> ISER1.bit6 */

    /* OLED：没接也不影响出声（初始化失败只把 oled_ok() 置 0，后续不再碰总线） */
    (void)oled_init();

    /* 开局先写静音中值，DAC 输出 1.65 V，喇叭安静 */
    g_left = g_right = 2048u;
    dac_flush();
    g_test_mode = 1u;                /* 上电即自检音，收到 FPGA 字自动退出 */

    uart_puts("\r\nF103 SPI-MCP4922 bridge v1.0 (all-SPI, no I2S)\r\n");
    uart_puts("LED-less; UART PA9 115200. Test tone until FPGA data arrives.\r\n");

    uint32_t last_report = 0, last_words = 0;
    uint32_t last_oled = 0, last_sig = 0xFFFFFFFFu;
    for (;;) {
        uint32_t now = g_ms;
#ifdef TONE_ONLY
        /* 临时诊断：钉死在自检音，永不切回直通 */
        g_test_mode = 1u;
#else
        /* 无 FPGA 超过 1 s → 自检音；有数据 → 直通。切模式无 racing：
           dac_write/dac_flush 内部已原子化 */
        uint8_t want_test = ((now - g_last_word_ms) > 1000u) ? 1u : 0u;
        if (want_test != g_test_mode) {
            uint32_t pm = primask_save();
            irq_disable();
            g_test_mode = want_test;
            if (!want_test) { g_phase = 0; }
            if (!pm) { irq_enable(); }
        }
#endif

        if (now - last_report >= 500u) {
            last_report = now;
            uint32_t dw = g_words - last_words;
            last_words = g_words;
            uart_puts("mode=");
            uart_puts(g_test_mode ? "TEST(440Hz)" : "PASS");
            uart_puts(" words/s=");
            uart_putdec(dw * 2u);          /* 500ms 窗口 ×2 */
            uart_puts(" fs=");
            uart_putdec(dw);               /* 每秒声道对数 ≈ 采样率 */
            uart_puts(" ovr=");
            uart_putdec(g_ovr);
            uart_puts(" cfg=");
            uart_putdec(g_cfg_ok);
            uart_puts("/");
            uart_putdec(g_cfg_bad);
            uart_puts(" oled=");
            uart_putdec(oled_ok() ? 1u : 0u);
            uart_puts("\r\n");
        }

        /* OLED：内容变了立刻刷，没变每 3 秒补一次（防止上电时序导致花屏） */
        {
            uint32_t sig = oled_sig();
            if (sig != last_sig || (now - last_oled) >= 3000u) {
                last_sig = sig;
                last_oled = now;
                oled_refresh();
            }
        }
    }
}
