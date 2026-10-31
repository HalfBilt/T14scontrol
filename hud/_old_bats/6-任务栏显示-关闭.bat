@echo off
setlocal
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'hud\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo 已关闭任务栏显示（下次双击“开关”即可再打开）。
timeout /t 2 >nul
