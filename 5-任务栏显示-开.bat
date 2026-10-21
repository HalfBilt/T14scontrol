@echo off
setlocal
cd /d "%~dp0"

set "PYW=%~dp0..\runtime\pythonw.exe"
if not exist "%PYW%" (
    echo [错误] 找不到共用运行时: %PYW%
    echo 请确认 T14scontrol\runtime\pythonw.exe 还在。
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0hud.py"
echo 任务栏显示已启动（已在运行则不会重复开）。
echo 右键点任务栏上的读数块调菜单；也可右键托盘里的三叶风扇图标。
echo 注意：直接双击 hud.py 会报错（.py 没有文件关联），请用本 bat。
timeout /t 3 >nul
