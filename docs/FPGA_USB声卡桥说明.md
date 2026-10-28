# ACX720 FPGA 澹板崱妗ワ紙UART 24.414 kHz 绔嬩綋澹帮級

> **2026-10-20 23:25 实测修正：本机 COM4 就是 ACX720 板载 CH340（FPGA UART），
> 不是 STM32。** 用 `fpga_uart_echo_check.py COM4` 回环自检：
> 发 1145 帧，收到 1144 个 0xA5，`RESULT: PASS`。
> 文中旧说法“COM4 是 STM32、COM3 才是 FPGA”已作废；桥默认端口已改为 COM4。

## 鏈€缁堟晥鏋?
Windows 澹伴煶璁剧疆 / 鎾斁璁惧閲屽鍑轰竴涓細

```
鎵０鍣?(VB-Audio Virtual Cable)
鎴?CABLE Input (VB-Audio Virtual Cable)
```

閫変腑瀹冿紝鐢佃剳閲屾墍鏈夊０闊筹紙鎾斁鍣ㄣ€佹祻瑙堝櫒銆佷笂浣嶆満鏂囦欢妯″紡銆丄I 鍒嗙缁撴灉锛?閮戒細璧帮細

```
Windows 鎾斁璁惧 (CABLE Input)
  鈹斺攢 VB-CABLE 鍐呴儴鐜矾
       鈹斺攢 CABLE Output (褰曢煶渚?
            鈹斺攢 fpga_audio_bridge.py 閲囬泦 + 閲嶉噰鏍峰埌 24.414 kHz/16bit/stereo
                 鈹斺攢 COM4 (ACX720 鏉胯浇 CH340, 1.5 Mbaud)
                      鈹斺攢 FPGA UART_RX (J21)
                           鈹斺攢 24.414 kHz 闊抽澶勭悊閾?                                鈹斺攢 SPI A13/A14/A15 -> STM32 PA5/PA4/PA7
                                     鈹斺攢 SPI2 -> MCP4922 -> PAM8403 -> 鍠囧彮
```

**杩欎笉鏄?STM32 鍘熺敓鐨?USB 澹板崱**銆係TM32 娌℃湁 USB Audio 鍥轰欢锛?杩欓噷鐢?VB-CABLE 鍦?Windows 渚ч€犱竴涓櫄鎷熸挱鏀捐澶囷紝妗ユ帴绋嬪簭鎶婂畠閫佽繘 FPGA UART銆?鎵€浠ワ細
- 璁惧鏍忛噷鍑虹幇鐨勬槸 `VB-Audio Virtual Cable`锛屽彲浠ラ噸鍛藉悕涓?鈥淎CX720 FPGA Speaker鈥濄€?- 闊抽纭疄缁忚繃 FPGA锛屽啀鍒?STM32/DAC/鍠囧彮銆?- 24.414 kHz 绔嬩綋澹?16bit锛屾濂芥槸宸ョ▼鍘熺敓閲囨牱鐜囷紝涓嶉渶瑕?FPGA 鍐嶉噸閲囨牱銆?
## 鍏堢‘璁?COM 鍙?
ACX720 涓婃湁涓や釜 USB锛?
| 鏉夸笂鐨勫彛 | 鑺墖 | 鐢ㄩ€?| Windows 璁惧 |
|---|---|---|---|
| **USB2.0** | CY7C68013A (FX2) | FPGA 楂橀€熸暟鎹?| 闇€瑕?FX2 鍥轰欢锛?*涓嶆槸澹板崱** |
| **USB-UART** | CH340E | FPGA 涓插彛 | 涓€涓?COM 鍙ｏ紙鏈満涔嬪墠鏄?COM4锛?|

**UART 闊抽蹇呴』鐢?USB-UART 鍙ｏ紝涓嶆槸 USB2.0 鍙ｃ€?*

鏈満褰撳墠鏋氫妇锛?
- `COM4`锛氬拰 ST-Link 鎸傚湪鍚屼竴涓?USB Hub锛圥ort1/Port3锛夛紝鏄?**STM32 鐨?USB-TTL**锛屼笉鏄?FPGA銆?- `COM4`锛氫箣鍓嶅嚭鐜拌繃銆佺幇鍦ㄧ伆鑹茬殑 CH340锛岀埗璁惧鏄牴闆嗙嚎鍣紝鎵嶆槸 **ACX720 鏉胯浇 CH340**銆?
鎵€浠ワ細**鎶?ACX720 鏉夸笂鏍?USB-UART / CH340 鐨勫彛鐢?USB 绾挎帴鍒扮數鑴?*锛?COM4 浼氫粠鐏拌壊鍙樻垚鍙敤銆傛ˉ榛樿灏辨槸 COM4銆?
## 瀹夎/鍚姩

1. VB-CABLE 宸插畨瑁咃紙`VBCABLE_Setup_x64.exe`锛夈€?2. Windows 澹伴煶璁剧疆 鈫?杈撳嚭 鈫?閫?**鎵０鍣?(VB-Audio Virtual Cable)**銆?   - 鎯虫敼鍚嶏細璁剧疆 鈫?绯荤粺 鈫?澹伴煶 鈫?鎵€鏈夊０闊宠澶?鈫?璇ヨ澶?鈫?閲嶅懡鍚嶃€?3. 鍙屽嚮 `tool\鍚姩FPGA澹板崱妗?bat`锛屼繚鎸佺獥鍙ｈ繍琛屻€?4. 鎾斁浠绘剰澹伴煶锛屽崟鐗囨満鍠囧彮搴斿嚭澹般€?
## 宸查獙璇?
- `rtl/uart_audio_rx.v` 浠跨湡锛歚ACTIVE=1 GOOD=1 CRCERR=0 VALID=68 FIRST_L=1000 FIRST_R=-1000 PASS`
- FPGA 浣嶆祦宸查噸鏂扮患鍚?瀹炵幇/鐑у綍锛?  - `End of startup status: HIGH`
  - `PROGRAM DONE`
- STM32 SPI 宸茶兘鏀跺埌瀛楋細`g_words 鈮?54000/s`锛宍fs鈮?4414`銆?- VB-CABLE 宸插畨瑁咃紝`CABLE Input` 鍑虹幇鍦ㄦ挱鏀捐澶囬噷銆?- 妗ユ帴绋嬪簭 `--dry-run` 瀹炴祴锛?4100 Hz 閲囬泦 鈫?24.414 kHz锛宍frames鈮?81/s`锛? dropped銆?
## 鏁呴殰鎺掓煡

| 鐜拌薄 | 妫€鏌?|
|---|---|
| 璁惧鏍忔病鏈?CABLE Input | 閲嶈 VB-CABLE锛岄噸鍚?Windows Audio |
| 妗ユ彁绀烘壘涓嶅埌 CABLE Output | 澹伴煶璁剧疆閲屽厛閫変竴娆?CABLE Input锛屽啀閲嶅惎妗?|
| `COM4` 鎵撲笉寮€ | ACX720 USB-UART 娌℃彃锛屾垨浠嶆槸鐏拌壊骞界伒锛涙嫈鎻掔湅绔彛鍙?|
| 妗ュ湪璺戜絾鍠囧彮鏃犲０ | 鏌?FPGA鈫扴TM32 鍥涙牴 SPI 绾匡紙A13/PA5銆丄14/PA4銆丄15/PA7銆丟ND锛?|
| 鏈夊櫔闊?鏂画 | 闄嶄綆 Windows 杈撳嚭闊抽噺锛涚‘璁?1.5 Mbaud锛涚缉鐭?USB 绾?|
| 鎯虫仮澶嶆澘杞?codec 杈撳叆 | 鍏虫帀妗ワ紝100 ms 鍚?FPGA 鑷姩鍥為€€鍒?ES8388 ADC |

## 鏂囦欢

- `tool/fpga_audio_bridge.py`锛歏B-CABLE 鈫?閲嶉噰鏍?鈫?甯?鈫?COM4
- `tool/鍚姩FPGA澹板崱妗?bat`锛氫竴閿惎鍔?- `tool/fpga_uart_tone.py`锛氫笉渚濊禆 VB-CABLE 鐨?440 Hz 娴嬭瘯闊?- `rtl/uart_rx.v` / `rtl/uart_audio_rx.v`锛欶PGA UART 闊抽鎺ユ敹
- `tool/build_uart_audio.tcl`锛歏ivado 鏋勫缓鑴氭湰

