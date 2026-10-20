@echo off
rem ---- console codepage adaptive: keep Python output readable ----
for /f "tokens=2 delims=: " %%c in ('chcp') do set "CP=%%c"
if "%CP%"=="936" set "PYTHONIOENCODING=gbk"
if "%CP%"=="65001" set "PYTHONIOENCODING=utf-8"
setlocal
cd /d "%~dp0"
set "PYW=%~dp0..\runtime\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
start "" "%PYW%" "%~dp0hud\hud.py"
echo 任务栏显示已启动（若已在运行则不会重复开）。
echo 右键点任务栏上的小方块，可切换显示项、开机自启、隐藏/退出。
timeout /t 2 >nul
