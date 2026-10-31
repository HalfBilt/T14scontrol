@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
title 风扇服务 - 重启

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

rem ========== 提权（net session 比 fltmc 可靠）==========
net session >nul 2>&1
if errorlevel 1 (
  echo 正在请求管理员权限，请在弹窗中点是...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "FANCTL=%ROOT%\fan\src\fan_service_ctl.py"
echo ==========================================
echo   重启风扇服务
echo ==========================================
echo [1/3] 停止
"%PY%" "%FANCTL%" stop
timeout /t 2 /nobreak >nul
echo [2/3] 启动
"%PY%" "%FANCTL%" start
echo [3/3] 状态
"%PY%" "%FANCTL%" status
echo.
pause
