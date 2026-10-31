@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
title HUD - 解锁并显示

rem ========== 定位安装根目录（三级兜底 + 路径规范化）==========
set "ROOT="
if exist "%~dp0..\runtime\python.exe" for %%A in ("%~dp0..") do set "ROOT=%%~fA"
if not defined ROOT if exist "%APPDATA%\T14scontrol\runtime\python.exe" set "ROOT=%APPDATA%\T14scontrol"
if not defined ROOT if exist "C:\Users\Ting\AppData\Roaming\T14scontrol\runtime\python.exe" set "ROOT=C:\Users\Ting\AppData\Roaming\T14scontrol"
if not defined ROOT (
  echo [X] 找不到 runtime\python.exe
  echo     应在 %%APPDATA%%\T14scontrol\runtime\
  pause
  exit /b 1
)
set "PY=%ROOT%\runtime\python.exe"
set "PYW=%ROOT%\runtime\pythonw.exe"
set "HUDPY=%ROOT%\hud\hud.py"
start "" "%PYW%" "%HUDPY%" --cmd unlock
echo 已解锁并显示（若本来没运行，会直接启动）
timeout /t 2 >nul
