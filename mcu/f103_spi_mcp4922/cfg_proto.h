/* ============================================================================
 *  cfg_proto.h —— 上位机 → STM32 的「工作模式」配置包协议（双向唯一事实源）
 *
 *  为什么单独拆一个文件：这套协议有三处实现（上位机写 mode.json、声卡桥打包、
 *  单片机解包）。任何一处字节偏移写错，现象都只是"屏幕上数字不对"，很难看出来。
 *  拆出来以后可以用 PC 上的编译器直接跑单元测试（tool/test_cfg_proto.c），
 *  不需要接板子。
 *
 *  帧格式（串口 1.5 Mbaud，夹在音频帧之间）：
 *      AA 55 | LEN | TYPE | payload[LEN] | CRC8
 *      CRC8：poly 0x07，init 0x00，覆盖 LEN / TYPE / payload
 *      TYPE=1：模式快照，LEN=11
 *          [0] mode     0=变调 1=分离
 *          [1] engine   0=FPGA 直算 1=中置提取 2=本地模型
 *          [2] dphase   有符号（(semi-5)*6 的等价量）
 *          [3..4] semi10    半音 ×10，int16 小端
 *          [5..6] ratio1000 变调比 ×1000，uint16 小端（1.297x 存 1297）
 *          [7] vocal_gain %  [8] acc_gain %
 *          [9] gain_db 有符号 [10] src 0=实时 1=文件
 * ==========================================================================*/
#ifndef CFG_PROTO_H
#define CFG_PROTO_H

#include <stdint.h>

#define CFG_MAGIC0      0xAA
#define CFG_MAGIC1      0x55
#define CFG_TYPE_MODE   1
#define CFG_PAYLOAD     11

typedef struct {
    uint8_t  mode;
    uint8_t  engine;
    int8_t   dphase;
    int16_t  semi10;
    uint16_t ratio1000;
    uint8_t  vgain;
    uint8_t  again;
    int8_t   gain_db;
    uint8_t  src;
    uint32_t stamp;        /* 由调用方填（MCU 里填 g_ms） */
} cfg_t;

uint8_t cfg_crc8(const uint8_t *p, uint32_t n);

/* 喂一个字节；凑出**校验通过**的完整 TYPE=1 包时写 *out 并返回 1，否则 0 */
int cfg_parse_byte(uint8_t b, cfg_t *out);

#endif
