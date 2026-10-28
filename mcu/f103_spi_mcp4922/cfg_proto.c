/* ============================================================================
 *  cfg_proto.c —— 配置包解析（纯逻辑，不碰任何寄存器，可在 PC 上单测）
 * ==========================================================================*/
#include "cfg_proto.h"

static uint8_t st;
static uint8_t plen, ptype, pidx;
static uint8_t pbuf[CFG_PAYLOAD];

uint8_t cfg_crc8(const uint8_t *p, uint32_t n)
{
    uint8_t c = 0;
    uint32_t i;
    int k;
    for (i = 0; i < n; i++) {
        c ^= p[i];
        for (k = 0; k < 8; k++) {
            c = (uint8_t)((c & 0x80u) ? (uint8_t)((uint8_t)(c << 1) ^ 0x07u)
                                      : (uint8_t)(c << 1));
        }
    }
    return c;
}

int cfg_parse_byte(uint8_t b, cfg_t *out)
{
    uint8_t tmp[CFG_PAYLOAD + 2];
    uint32_t i;

    switch (st) {
    case 0:
        if (b == CFG_MAGIC0) { st = 1; }
        break;
    case 1:
        st = (uint8_t)((b == CFG_MAGIC1) ? 2 : ((b == CFG_MAGIC0) ? 1 : 0));
        break;
    case 2:
        plen = b;
        st = (uint8_t)((plen == CFG_PAYLOAD) ? 3 : 0);
        break;
    case 3:
        ptype = b;
        pidx = 0;
        st = 4;
        break;
    case 4:
        pbuf[pidx++] = b;
        if (pidx >= plen) { st = 5; }
        break;
    case 5:
        st = 0;
        tmp[0] = plen;
        tmp[1] = ptype;
        for (i = 0; i < CFG_PAYLOAD; i++) { tmp[2 + i] = pbuf[i]; }
        if (ptype != CFG_TYPE_MODE) { break; }
        if (cfg_crc8(tmp, CFG_PAYLOAD + 2u) != b) { break; }
        if (out) {
            out->mode     = pbuf[0];
            out->engine   = pbuf[1];
            out->dphase   = (int8_t)pbuf[2];
            out->semi10   = (int16_t)((uint16_t)pbuf[3] | ((uint16_t)pbuf[4] << 8));
            out->ratio1000 = (uint16_t)((uint16_t)pbuf[5] | ((uint16_t)pbuf[6] << 8));
            out->vgain    = pbuf[7];
            out->again    = pbuf[8];
            out->gain_db  = (int8_t)pbuf[9];
            out->src      = pbuf[10];
        }
        return 1;
    default:
        st = 0;
        break;
    }
    return 0;
}
