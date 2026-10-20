@echo off
setlocal
echo 正在关闭任务栏显示...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'hud\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo 已关闭。
timeout /t 1 >nul
