@echo off
chcp 65001 >nul
echo 正在关闭 FanControl（只关风扇工具，不动 CPU 功耗墙服务）...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*fan_control.py*' } | ForEach-Object { Write-Host ('  结束 PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
echo 完成。风扇会停在最后一个档位（不会自动交还 EC）。
timeout /t 3 >nul
