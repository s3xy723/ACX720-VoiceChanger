/* ============================================================================
 *  oled.c —— SSD1306 128x64 I2C 驱动（STM32F103，裸寄存器，无库）
 *
 *  I2C1 硬件外设：PB6=SCL、PB7=SDA，400 kHz（PCLK1 = 32 MHz）。
 *  没有用中断/DMA：一帧 1024 字节一次推送约 25 ms，主循环里干这件事完全
 *  可以接受 —— 音频那两条 SPI 全在中断里跑，I2C 慢一点不影响出声音。
 *  每次事务都带超时，总线被拉死时不会把整个固件卡住。
 * ==========================================================================*/
#include <stdint.h>
#include "oled.h"
#include "oled_font.h"

#define REG32(a)        (*(volatile uint32_t *)(a))

#define RCC_APB1ENR     REG32(0x40021000u + 0x1C)
#define RCC_APB2ENR     REG32(0x40021000u + 0x18)

#define GPIOB_CRL       REG32(0x40010C00u + 0x00)

#define I2C1_CR1        REG32(0x40005400u + 0x00)
#define I2C1_CR2        REG32(0x40005400u + 0x04)
#define I2C1_OAR1       REG32(0x40005400u + 0x08)
#define I2C1_DR         REG32(0x40005400u + 0x10)
#define I2C1_SR1        REG32(0x40005400u + 0x14)
#define I2C1_SR2        REG32(0x40005400u + 0x18)
#define I2C1_CCR        REG32(0x40005400u + 0x1C)
#define I2C1_TRISE      REG32(0x40005400u + 0x20)

#define I2C_CR1_SWRST   (1u << 15)
#define I2C_CR1_SMBUS   (1u << 1)
#define I2C_CR1_PE      (1u << 0)
#define I2C_CR2_FREQ    (32u)                 /* APB1 = 32 MHz */
#define I2C_CR2_FREQ_M  (0x3Fu << 0)
#define I2C_CR1_START   (1u << 8)
#define I2C_CR1_STOP    (1u << 9)
#define I2C_CR1_ACK     (1u << 10)
#define I2C_SR1_SB      (1u << 0)
#define I2C_SR1_ADDR    (1u << 1)
#define I2C_SR1_BTF     (1u << 2)
#define I2C_SR1_RXNE    (1u << 6)
#define I2C_SR1_TXE     (1u << 7)
#define I2C_SR1_AF      (1u << 10)
#define I2C_SR2_MSL     (1u << 0)
#define I2C_SR2_BUSY    (1u << 1)

#define OLED_CMD        0x00
#define OLED_DATA       0x40

static unsigned char fb[OLED_PAGES][OLED_W];
static uint8_t  addr_ok;        /* 0 = 没找到屏 */
static uint8_t  last_ok = 1;    /* 上一次事务是否成功 */
static uint32_t err_cnt;

/* ------------------------------------------------------------ 基础 I2C */
static int wait_flag(volatile uint32_t *reg, uint32_t mask, int want_set)
{
    uint32_t n = 200000u;                 /* 32 MHz 下约 6 ms 超时 */
    while (n--) {
        uint32_t v = *reg;
        if (((v & mask) != 0u) == (want_set != 0)) {
            return 0;
        }
    }
    return -1;
}

static void i2c1_init(void)
{
    RCC_APB1ENR |= (1u << 21);            /* I2C1EN */
    RCC_APB2ENR |= (1u << 3);             /* IOPBEN */

    /* PB6/PB7 = 复用开漏 50MHz（0xF） */
    GPIOB_CRL = (GPIOB_CRL & ~0xFF000000u) | 0xFF000000u;

    I2C1_CR1 |= I2C_CR1_SWRST;
    I2C1_CR1 &= ~I2C_CR1_SWRST;
    I2C1_CR1 &= ~I2C_CR1_SMBUS;
    I2C1_CR2 = (I2C1_CR2 & ~I2C_CR2_FREQ_M) | I2C_CR2_FREQ;
    /* 400 kHz：CCR = PCLK1 / (2*F) = 32e6/(2*400k) = 40（DUTY=0 即 1:1） */
    I2C1_CCR = 40u;
    I2C1_TRISE = 10u;                     /* 300 ns * 32 MHz + 1 */
    I2C1_CR1 |= I2C_CR1_PE;
}

/* 一次完整写事务：addr + n 字节。返回 0 = 全部 ACK */
static int i2c_write(uint8_t addr, const uint8_t *buf, uint32_t n)
{
    uint32_t i;
    if (I2C1_SR2 & I2C_SR2_BUSY) {
        /* 总线忙：先发 STOP 复位，再等它闲下来 */
        I2C1_CR1 |= I2C_CR1_STOP;
        if (wait_flag(&I2C1_SR2, I2C_SR2_BUSY, 0) != 0) {
            return -1;
        }
    }
    I2C1_CR1 |= I2C_CR1_START;
    if (wait_flag(&I2C1_SR1, I2C_SR1_SB, 1) != 0) {
        return -1;
    }
    I2C1_DR = (uint32_t)(addr << 1);
    if (wait_flag(&I2C1_SR1, I2C_SR1_ADDR, 1) != 0) {
        I2C1_CR1 |= I2C_CR1_STOP;
        return -1;
    }
    (void)I2C1_SR2;                       /* 清 ADDR：读 SR1 再读 SR2 */

    for (i = 0; i < n; i++) {
        if (wait_flag(&I2C1_SR1, I2C_SR1_TXE, 1) != 0) {
            I2C1_CR1 |= I2C_CR1_STOP;
            return -1;
        }
        I2C1_DR = buf[i];
    }
    if (wait_flag(&I2C1_SR1, I2C_SR1_BTF, 1) != 0) {
        I2C1_CR1 |= I2C_CR1_STOP;
        return -1;
    }
    I2C1_CR1 |= I2C_CR1_STOP;
    return 0;
}

/* 写一段控制字节 + 数据。ctrl=0x00 发命令，0x40 发显存 */
static int oled_send(uint8_t ctrl, const uint8_t *buf, uint32_t n)
{
    static uint8_t tmp[OLED_W + 1];
    uint32_t chunk;
    uint32_t off = 0;
    while (off < n) {
        chunk = n - off;
        if (chunk > OLED_W) {
            chunk = OLED_W;
        }
        tmp[0] = ctrl;
        {
            uint32_t i;
            for (i = 0; i < chunk; i++) {
                tmp[1 + i] = buf[off + i];
            }
        }
        if (i2c_write(OLED_ADDR, tmp, chunk + 1u) != 0) {
            last_ok = 0;
            err_cnt++;
            return -1;
        }
        off += chunk;
    }
    last_ok = 1;
    return 0;
}

static int oled_cmd(uint8_t c)
{
    return oled_send(OLED_CMD, &c, 1);
}

/* ------------------------------------------------------------ 显存绘制 */
void oled_clear(void)
{
    uint32_t p, x;
    for (p = 0; p < OLED_PAGES; p++) {
        for (x = 0; x < OLED_W; x++) {
            fb[p][x] = 0;
        }
    }
}

static void put_col(int page, int x, unsigned char lo, unsigned char hi)
{
    if (x < 0 || x >= OLED_W || page < 0 || page >= OLED_PAGES) {
        return;
    }
    fb[page][x] = lo;
    if (page + 1 < OLED_PAGES) {
        fb[page + 1][x] = hi;
    }
}

/* 8x16 ASCII：每个字符 8 列，占 2 页 */
void oled_text(int page, int x, const char *s)
{
    while (*s) {
        unsigned char c = (unsigned char)*s++;
        int idx;
        if (c < FONT_ASCII_FIRST || c > 127) {
            c = '?';
        }
        idx = c - FONT_ASCII_FIRST;
        {
            int i;
            for (i = 0; i < 8; i++) {
                put_col(page, x + i, FONT8X16[idx][i], FONT8X16[idx][8 + i]);
            }
        }
        x += 8;
    }
}

/* 16x16 汉字：按码点查表，查不到画一个方框 */
void oled_cjk(int page, int x, unsigned int code)
{
    int i, k;
    const unsigned char *m = 0;
    for (k = 0; k < CJK16_COUNT; k++) {
        if (CJK16[k].code == code) {
            m = CJK16[k].mat;
            break;
        }
    }
    for (i = 0; i < 16; i++) {
        if (m) {
            put_col(page, x + i, m[i], m[16 + i]);
        } else {
            /* 缺字：画空心框，一眼看出是字库没收录而不是屏幕坏 */
            put_col(page, x + i,
                    (unsigned char)((i == 0 || i == 15) ? 0xFF : 0x81),
                    (unsigned char)((i == 0 || i == 15) ? 0xFF : 0x81));
        }
    }
}

/* ------------------------------------------------------------ 上屏 */
void oled_flush(void)
{
    uint8_t cmds[6];
    uint32_t p;
    cmds[0] = 0x21; cmds[1] = 0x00; cmds[2] = 0x7F;   /* 列 0..127 */
    cmds[3] = 0x22; cmds[4] = 0x00; cmds[5] = 0x07;   /* 页 0..7 */
    if (oled_send(OLED_CMD, cmds, 6) != 0) {
        return;
    }
    for (p = 0; p < OLED_PAGES; p++) {
        if (oled_send(OLED_DATA, fb[p], OLED_W) != 0) {
            return;
        }
    }
}

int oled_ok(void) { return addr_ok && last_ok; }
unsigned int oled_err_count(void) { return err_cnt; }

/* ------------------------------------------------------------ 初始化 */
int oled_init(void)
{
    static const uint8_t seq[] = {
        0xAE,             /* 关显示 */
        0xD5, 0x80,       /* 时钟分频 */
        0xA8, 0x3F,       /* 复用率 64 */
        0xD3, 0x00,       /* 显示偏移 0 */
        0x40,             /* 起始行 0 */
        0x8D, 0x14,       /* 电荷泵开 */
        0x20, 0x00,       /* 水平寻址 */
        0xA1,             /* 段重映射（左右不倒） */
        0xC8,             /* COM 反向扫描（上下不倒） */
        0xDA, 0x12,       /* COM 引脚配置 */
        0x81, 0x8F,       /* 对比度 */
        0xD9, 0xF1,       /* 预充电 */
        0xDB, 0x40,       /* VCOMH */
        0xA4,             /* 显示跟随显存 */
        0xA6,             /* 正常（非反色） */
        0xAF              /* 开显示 */
    };

    i2c1_init();
    oled_clear();
    addr_ok = 0;

    /* 先探一下地址：0x3C 不通就当它没接 */
    {
        uint8_t dummy = 0xAE;
        if (i2c_write((uint8_t)OLED_ADDR, &dummy, 1) != 0) {
            last_ok = 0;
            err_cnt++;
            return -1;
        }
    }
    addr_ok = 1;

    {
        uint32_t i = 0;
        while (i < sizeof(seq)) {
            if (oled_cmd(seq[i++]) != 0) {
                addr_ok = 0;
                return -2;
            }
        }
    }
    oled_flush();
    return 0;
}
