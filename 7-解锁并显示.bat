@echo off
setlocal
cd /d "%~dp0"
set "PYW=%~dp0..\runtime\pythonw.exe"
if not exist "%PYW%" (
    echo [错误] 找不到共用运行时: %PYW%
    pause
    exit /b 1
)
rem 关掉"点击穿透"和"锁定拖动"，并强制显示
start "" "%PYW%" "%~dp0hud.py" --cmd unlock
echo 已解锁并显示（如果它本来没在运行，会直接启动）。
timeout /t 2 >nul
