@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title 重启 FanControl

fltmc >nul 2>&1
if %errorlevel%==0 goto run

echo 正在请求管理员权限 ...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:run
echo ============================================================
echo   重启 FanControl（只关风扇工具，不动 CPU 功耗墙服务）
echo ============================================================
echo.
echo 正在停止旧进程 ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*fan_control.py*' } | ForEach-Object { Write-Host ('  结束 PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
ping -n 3 127.0.0.1 >nul

echo 正在启动 ...
start "" "%~dp0runtime\pythonw.exe" "%~dp0src\fan_control.py"
ping -n 7 127.0.0.1 >nul
echo.
echo   已启动。
echo   看托盘图标是否变绿（智能模式）。
echo   确认 DTS 通道是否就绪：
echo     findstr /C:"DTS 通道" "%~dp0fan_control.log"
echo.
pause
exit /b 0
