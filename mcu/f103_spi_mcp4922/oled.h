/* ============================================================================
 *  oled.h —— 0.96" SSD1306 128x64（I2C，黑白）在 STM32F103 上的驱动接口
 *
 *  接线（Blue Pill / STM32F103C8T6）：
 *      OLED VCC  -> 3.3V
 *      OLED GND  -> GND
 *      OLED SCL  -> PB6   (I2C1_SCL，板载模块自带 4.7k 上拉)
 *      OLED SDA  -> PB7   (I2C1_SDA)
 *
 *  为什么放 MCU 不放 FPGA：FPGA 侧要显示文字就得带字库 + 显存 + 一个 I2C
 *  主机，得重新综合、重新烧；MCU 这边 Flash 有 64 KB 放字库、RAM 有 20 KB
 *  放显存，改一行代码就能改界面。而且 I2C 是独立的总线，跟音频那两条 SPI
 *  完全不抢引脚。
 * ==========================================================================*/
#ifndef OLED_H
#define OLED_H

/* I2C 从地址：0.96" 模块常见 0x3C（有的 0x3D），这里两者都试 */
#define OLED_ADDR       0x3C
#define OLED_W          128
#define OLED_PAGES      8

/* 初始化（I2C1 + SSD1306 命令序列）。返回 0 = 屏幕有应答，非 0 = 没应答 */
int  oled_init(void);

/* 直接操作显存（页 0..7，列 0..127）。改完要 oled_flush() 才上屏 */
void oled_clear(void);
void oled_text(int page, int x, const char *s);      /* 8x16 ASCII */
void oled_cjk(int page, int x, unsigned int code);   /* 16x16 汉字（码点） */

/* 把整屏 1024 字节推给 SSD1306（约 25 ms @400 kHz） */
void oled_flush(void);

/* 最近一次 I2C 事务是否成功（1=OK，0=失败）。用来在界面上判断
   "是屏幕没接好还是程序没跑到" —— 没屏也能通过 UART/调试器读出来。 */
int  oled_ok(void);
unsigned int oled_err_count(void);

#endif
