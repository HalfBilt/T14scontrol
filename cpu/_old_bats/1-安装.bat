@echo off
setlocal EnableExtensions
chcp 936 >nul 2>&1
set "PYTHONIOENCODING=gbk"
title CpuPLFix - 安装

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
echo   安装 CpuPLFix 服务
echo ==========================================
echo   根目录: %ROOT%
echo.
"%PY%" "%SCRIPT%" install
echo.
echo ------------------------------------------
echo 完成。预期: 服务 RUNNING / 功耗墙 PL1=28W
echo ------------------------------------------
pause
