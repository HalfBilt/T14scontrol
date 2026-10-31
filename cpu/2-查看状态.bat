@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
title CpuPLFix - 查看状态

rem ========== 定位安装根目录（三级兜底，不依赖 %~dp0）==========
set "ROOT="
if exist "%~dp0..\runtime\python.exe" set "ROOT=%~dp0.."
if not defined ROOT if exist "%APPDATA%\T14scontrol\runtime\python.exe" set "ROOT=%APPDATA%\T14scontrol"
if not defined ROOT if exist "C:\Users\Ting\AppData\Roaming\T14scontrol\runtime\python.exe" set "ROOT=C:\Users\Ting\AppData\Roaming\T14scontrol"
if not defined ROOT (
  echo [X] 找不到安装目录 runtime\python.exe
  echo     应在 %%APPDATA%%\T14scontrol\runtime\
  pause
  exit /b 1
)

set "PY=%ROOT%\runtime\python.exe"
set "SCRIPT=%~dp0CpuPLFix.py"
if not exist "%SCRIPT%" set "SCRIPT=%ROOT%\cpu\CpuPLFix.py"
if not exist "%SCRIPT%" (
  echo [X] 找不到 CpuPLFix.py
  pause
  exit /b 1
)

rem ========== 提权（net session 比 fltmc 可靠）==========
net session >nul 2>&1
if errorlevel 1 (
  echo 正在请求管理员权限，请在弹窗中点是...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ==========================================
echo   查看当前状态
echo ==========================================
echo   根目录: %ROOT%
echo.
"%PY%" "%SCRIPT%" check
echo.
echo ------------------------------------------
echo 若功耗墙不是 28W 或服务异常，运行 1-安装.bat
echo ------------------------------------------
pause
