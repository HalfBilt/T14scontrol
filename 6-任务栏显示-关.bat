@echo off
setlocal
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'hud\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo 已关闭任务栏显示。
timeout /t 2 >nul
