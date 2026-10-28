@echo off
rem  ============================================================
rem   fft1024 one-shot verification: gen -> xvlog/xelab/xsim -> compare
rem
rem   NOTE: xelab has NO -work option (only -L).
rem   NOTE: keep ASCII only in this file; CP936 console mangles UTF-8.
rem  ============================================================
set VIVBIN=D:\Vivado\Vivado\2018.3\bin
set PY=C:\Users\s3xy\.workbuddy\binaries\python\envs\py314\Scripts\python.exe

echo ===== [1/3] gen stimulus =====
"%PY%" tool\gen_fft_tb_data.py
if errorlevel 1 goto :fail

echo ===== [2/3] xvlog + xelab + xsim =====
call "%VIVBIN%\xvlog.bat" -work work rtl\fft_twiddle.v rtl\fft1024.v sim_src\tb_fft1024.v
if errorlevel 1 goto :fail
call "%VIVBIN%\xelab.bat" tb_fft1024 -s tb_fft_sim --timescale 1ns/1ps --nolog
if errorlevel 1 goto :fail
call "%VIVBIN%\xsim.bat" tb_fft_sim -R --nolog
if errorlevel 1 goto :fail

echo ===== [3/3] compare vs fixed-point model =====
"%PY%" tool\check_fft_sim.py
goto :eof

:fail
echo FAILED
exit /b 1
