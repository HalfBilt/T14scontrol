@echo off
setlocal
cd /d "%~dp0"
set "PYW=%~dp0..\runtime\pythonw.exe"
if not exist "%PYW%" (
    echo [错误] 找不到共用运行时: %PYW%
    pause
    exit /b 1
)
rem 没在运行 -> 启动；已在运行 -> 切换显示/隐藏
start "" "%PYW%" "%~dp0hud.py" --toggle
timeout /t 1 >nul
