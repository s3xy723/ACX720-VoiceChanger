/* ============================================================================
 *  test_cfg_proto.c —— 在 PC 上单测配置包解析（不需要板子）
 *
 *  用法：  test_cfg_proto.exe "AA 55 0B 01 00 00 13 2E 00 82 00 64 64 00 01 12"
 *  输出：  mode=.. engine=.. dph=.. semi10=.. ratio100=.. vg=.. ag=.. g=.. src=..
 *          hits=<成功解析的次数>
 *
 *  由 tool/verify_oled_cfg.py 驱动：那边用**真正会发到串口的那段字节**
 *  （fpga_audio_bridge.build_cfg_packet 生成），这边用固件里那份
 *  cfg_proto.c 解析，两边对不上就说明协议实现漂了。
 * ==========================================================================*/
#include <stdio.h>
#include <string.h>

#include "../mcu/f103_spi_mcp4922/cfg_proto.h"

static int hexval(char c)
{
    if (c >= '0' && c <= '9') { return c - '0'; }
    if (c >= 'a' && c <= 'f') { return c - 'a' + 10; }
    if (c >= 'A' && c <= 'F') { return c - 'A' + 10; }
    return -1;
}

int main(int argc, char **argv)
{
    unsigned char buf[256];
    int n = 0, i, hits = 0;
    cfg_t c;
    const char *p;

    if (argc < 2) {
        printf("usage: %s \"AA 55 ...\"\n", argv[0]);
        return 2;
    }
    p = argv[1];
    while (*p && n < (int)sizeof(buf)) {
        int hi, lo;
        while (*p == ' ') { p++; }
        if (!*p) { break; }
        hi = hexval(*p++);
        lo = hexval(*p++);
        if (hi < 0 || lo < 0) { break; }
        buf[n++] = (unsigned char)((hi << 4) | lo);
    }

    memset(&c, 0, sizeof(c));
    for (i = 0; i < n; i++) {
        if (cfg_parse_byte(buf[i], &c)) { hits++; }
    }
    printf("mode=%d engine=%d dph=%d semi10=%d ratio100=%d vg=%d ag=%d g=%d src=%d hits=%d\n",
           c.mode, c.engine, c.dphase, c.semi10, c.ratio100,
           c.vgain, c.again, c.gain_db, c.src, hits);
    return hits > 0 ? 0 : 1;
}
