`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 2024/08/07 10:50:35
// Design Name: 
// Module Name: ES8388_init_table
// Project Name: 
// Target Devices: 
// Tool Versions: 
// Description: 
// 
// Dependencies: 
// 
// Revision:
// Revision 0.01 - File Created
// Additional Comments:
// 
//////////////////////////////////////////////////////////////////////////////////


module ES8388_init_table#(
    parameter DATA_WIDTH=16, 
    parameter ADDR_WIDTH=8
)(
	input [(ADDR_WIDTH-1):0] addr,
	input clk, 
	output reg [(DATA_WIDTH-1):0] q,
	output [7:0]dev_id,
	output [7:0]lut_size
);

	reg [DATA_WIDTH-1:0] rom[2**ADDR_WIDTH-1:0];
	
	assign dev_id = 8'h20;		// ES8388IIC接口器件地址
	assign lut_size = 8'd43;	//ES8388 寄存器初始化数量

	// ------------------------------------------------------------------
	//  本文件是芯路恒(小梅哥) ACX720-V3 官方例程 ch35_CoreCourse_GHRD_I2S_RX
	//  的原文件，内容一字未改（只把注释从 GBK 转成了 UTF-8）。
	//
	//  MCLK：官方例程用 PLL 产生 8.192 MHz（-> LRCK = 16 kHz）。
	//        本工程改成 50 MHz 四分频的 12.5 MHz，于是
	//            LRCK = 12.5M / 512      = 24.414 kHz（实际采样率）
	//            BCLK = 12.5M / 8        =  1.5625 MHz = 64 * LRCK
	//        codec 只做整数分频，对 MCLK 的绝对精度不敏感，所以能直接用。
	//        想回到 16 kHz / 48 kHz，只要换 MCLK 频率，本表不用动。
	//
	//  输入通道：rom[12](0x0A) / rom[13](0x0B) 现在是 LIN1/RIN1（Line IN）。
	//        若你的板子把麦克风接在 LIN2/RIN2，改成
	//            rom[12] = 16'h0A_52;   rom[13] = 16'h0B_82;
	// ------------------------------------------------------------------
	//MCLK时钟为8.192M
	//Line IN	
	always @ (*) begin
		/*
			设置为主模式(0x08寄存器的bit[7]为1)，FPGA提供给音频芯片MCLK，本次实验我们设置MCLK为8.192M
			寄存器0x0D的[4:0]和寄存器0x18的[4:0]控制MCLK/LRCK的比例,比如我们设置为0x04，则LRCK=MCLK/512=16K
			寄存器0x08的[4:0]控制MCLK/SCLK的比例,
			同时还需要考虑到ADC和DAC的位数,如果16bit，此时SCLK需要至少 =16K*32 =512K ,MCLK/16 =512K,则0x08就需要设置为0x8A
			16bit:SCLK=32*LRCK
			18bit:SCLK=36*LRCK
			20bit:SCLK=40*LRCK
			24bit:SCLK=48*LRCK
			32bit:SCLK=64*LRCK
		*/
		
		rom[0 ] = 16'h01_58; 
		rom[1 ] = 16'h01_50;  
		rom[2 ] = 16'h02_F3;
		rom[3 ] = 16'h02_F0;
		rom[4 ] = 16'h2B_80; //ADC和DAC使用相同的LRCK bit[7] 为1
		rom[5 ] = 16'h00_36;
		rom[6 ] = 16'h08_84; //主模式控制寄存器：bit[7]：1：主机模式,控制MCLK/SCLK的比例bit【4：0】控制 SCLK为2.0148M
		rom[7 ] = 16'h04_00;
		rom[8 ] = 16'h0D_04; //配置MCLK和频率的比率      ADCLRCK=MCLK/对应比例（该寄存器配置[4：0]）512 16K
		rom[9 ] = 16'h18_04; 
		rom[10] = 16'h05_00;
		rom[11] = 16'h06_C3; 
		rom[12] = 16'h0A_00; //Select Analog input channel for ADC (Lin1/Rin1)    LIN1:0X00		LIN2：0x52
		rom[13] = 16'h0B_02; //(Select LIN1and RIN1 as differential input pairs)  LIN1:0X02    LIN2:0x82
		rom[14] = 16'h0C_0C; //ADC Control: [1:0]=00(I2S模式); [4:2]: 011: 16bit(0x0c); 000:24(0x00); 001:20(0x04); 010:18(0x08); 100:32(0x10)
		rom[15] = 16'h17_18; //DAC Control: [2:1]=00(I2S);     [5:3]: 011: 16bit(0x18); 000:24(0x00); 001:20(0x08); 010:18(0x10); 100:32(0x20)
		rom[16] = 16'h10_00; 
		rom[17] = 16'h11_00; 
		rom[18] = 16'h1A_00;
		rom[19] = 16'h1B_00;
		rom[20] = 16'h09_00;
		rom[21] = 16'h12_E2;
		rom[22] = 16'h13_C0;
		rom[23] = 16'h14_12;
		rom[24] = 16'h15_06;
		rom[25] = 16'h16_C3;
		rom[26] = 16'h27_B8;
		rom[27] = 16'h2A_B8;
		rom[28] = 16'h02_00;        //后面需要延时500ms
		rom[29] = 16'h2E_1E;
		rom[30] = 16'h2F_1E;
		rom[31] = 16'h30_1E;
		rom[32] = 16'h31_1E;
		rom[33] = 16'h04_36;    //0x30:使用OUT1 [4]:ROUT1 enable; [5]:LOUT1 enable; 0x06:使用OUT2 [2]:ROUT2 enable;[3]:LOUT2 enable
		rom[34] = 16'h26_00;
		rom[35] = 16'h03_09;
		rom[36] = 16'h2E_1E;
		rom[37] = 16'h2F_1E;
		rom[38] = 16'h30_1E;
		rom[39] = 16'h31_1E;
		rom[40] = 16'h32_00;
		rom[41] = 16'h33_aa;
		rom[42] = 16'h34_aa;

//		rom[0 ] = 16'h00_12; 
//		rom[1 ] = 16'h01_50;  
//		rom[2 ] = 16'h02_00;
//		rom[3 ] = 16'h03_00;
//		rom[4 ] = 16'h04_3c; 
//		rom[5 ] = 16'h05_00;
//		rom[6 ] = 16'h06_00; 
//		rom[7 ] = 16'h07_7c;
//		rom[8 ] = 16'h08_00; 
		
//		rom[9 ] = 16'h09_00; 
//		rom[10] = 16'h0a_00;
//		rom[11] = 16'h0b_02; 
//		rom[12] = 16'h0c_00; //Select Analog input channel for ADC (Lin1/Rin1)    LIN1:0X00		LIN2：0x52
//		rom[13] = 16'h0d_22; //(Select LIN1and RIN1 as differential input pairs)  LIN1:0X02    LIN2:0x82
//		rom[14] = 16'h0e_00; //ADC Control: [1:0]=00(I2S模式); [4:2]: 011: 16bit(0x0c); 000:24(0x00); 001:20(0x04); 010:18(0x08); 100:32(0x10)
//		rom[15] = 16'h0f_20; //DAC Control: [2:1]=00(I2S);     [5:3]: 011: 16bit(0x18); 000:24(0x00); 001:20(0x08); 010:18(0x10); 100:32(0x20)
//		rom[16] = 16'h10_00; 
//		rom[17] = 16'h11_00; 
//		rom[18] = 16'h12_e2;
//		rom[19] = 16'h13_a0;
//		rom[20] = 16'h14_12;
//		rom[21] = 16'h15_c3;
//		rom[22] = 16'h16_55;
		
		
//		rom[23] = 16'h17_18;
//		rom[24] = 16'h18_22;
//		rom[25] = 16'h19_00;
//		rom[26] = 16'h1a_00;
//		rom[27] = 16'h1b_00;
//		rom[28] = 16'h1c_08;       
//		rom[29] = 16'h1d_00;
//		rom[30] = 16'h1e_1f;
//		rom[31] = 16'h1f_f7;
//		rom[32] = 16'h20_fd;
//		rom[33] = 16'h21_ff;    
//		rom[34] = 16'h22_1f;
//		rom[35] = 16'h23_f7;
//		rom[36] = 16'h24_fd;
//		rom[37] = 16'h25_ff;
//		rom[38] = 16'h26_1b;
//		rom[39] = 16'h27_90;
//		rom[40] = 16'h28_28;
//		rom[41] = 16'h29_28;
//		rom[42] = 16'h2a_90;
//		rom[43] = 16'h2b_80;
//		rom[44] = 16'h2c_00;
//		rom[45] = 16'h2d_00;       
//		rom[46] = 16'h2e_1e;
//		rom[47] = 16'h2f_1e;
//		rom[48] = 16'h30_1e;
//		rom[49] = 16'h31_1e;
//		rom[50] = 16'h32_00;    
//		rom[51] = 16'h33_aa;
//		rom[52] = 16'h34_aa;

	end
	always @ (posedge clk)
	begin
		q <= rom[addr];
	end
endmodule
