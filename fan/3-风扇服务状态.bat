@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
title 风扇服务 - 状态

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
set "FANCTL=%ROOT%\fan\src\fan_service_ctl.py"set "FANLOG=%ROOT%\fan\fan_service.log"
"%PY%" "%FANCTL%" status
echo.
if exist "%FANLOG%" (
  echo ---- 最近 25 行日志 ----
  powershell -NoProfile -Command "Get-Content -LiteralPath '%FANLOG%' -Encoding UTF8 -Tail 25"
) else (
  echo 还没有日志文件: %FANLOG%
)
echo.
pause
