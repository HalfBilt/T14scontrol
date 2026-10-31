@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
title HUD - 关闭

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

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'hud\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo 已关闭任务栏显示（双击“5-任务栏显示-开关”可再打开）
timeout /t 2 >nul
