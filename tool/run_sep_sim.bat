@echo off
rem  ============================================================
rem   separation_core 一键验证：生成激励 -> xvlog/xelab/xsim -> 逐点对数
rem
rem   xelab **没有** -work 选项（只有 -L），写了会报 "unrecognised option"。
rem  ============================================================
set VIVBIN=D:\Vivado\Vivado\2018.3\bin
set PY=C:\Users\s3xy\.workbuddy\binaries\python\envs\py314\Scripts\python.exe

echo ===== [1/4] 生成激励 =====
"%PY%" tool\gen_sep_tb_data.py
if errorlevel 1 goto :fail

echo ===== [2/4] xvlog =====
call "%VIVBIN%\xvlog.bat" -work work rtl/fft_twiddle.v rtl/fft1024.v rtl/win1024.v rtl/pitch_recip.v rtl/separation_core.v sim_src/tb_separation.v
if errorlevel 1 goto :fail

echo ===== [3/4] xelab + xsim =====
call "%VIVBIN%\xelab.bat" tb_separation -s tb_sep_sim --timescale 1ns/1ps --nolog
if errorlevel 1 goto :fail
call "%VIVBIN%\xsim.bat" tb_sep_sim -R --nolog
if errorlevel 1 goto :fail

echo ===== [4/4] 与定点模型逐点对数 =====
"%PY%" tool\check_sep_sim.py
goto :eof

:fail
echo FAILED
exit /b 1
