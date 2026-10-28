`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 2024/08/07 10:55:11
// Design Name: 
// Module Name: ES8388_Init
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


module ES8388_Init(
	Clk,
	Rst_n,

	I2C_Init_Done,
	i2c_sdat,
	i2c_sclk
);

	input Clk;
	input Rst_n;
	
	inout i2c_sdat;
	output i2c_sclk;
	output I2C_Init_Done;

	//产生初始化使能信号
	reg [31:0]Delay_Cnt;
	reg Init_en;
	always@(posedge Clk or negedge Rst_n)
	begin
		if(!Rst_n)
			Delay_Cnt <= 'd0;
		else if(Delay_Cnt <  'd12000)
			Delay_Cnt <= Delay_Cnt + 8'd1;
		else
			Delay_Cnt <= Delay_Cnt;
	end	
	
	always@(posedge Clk or negedge Rst_n)
	begin
		if(!Rst_n)
			Init_en <= 1'b0;
		else if(Delay_Cnt == 'd11999)
			Init_en <= 1'b1;
		else
			Init_en <= 1'b0;
	end

	I2C_Init_Dev I2C_Init_Dev(
		.Clk(Clk),
		.Rst_n(Rst_n),
		.Go(Init_en),
		.Init_Done(I2C_Init_Done),
		.i2c_sclk(i2c_sclk),
		.i2c_sdat(i2c_sdat)
	);
endmodule
