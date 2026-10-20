@echo off
chcp 936 >nul
setlocal
title 修复风扇自启（电池限制）

fltmc >nul 2>&1
if %errorlevel%==0 goto run

echo 正在请求管理员权限 ...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:run
echo ============================================================
echo   修复 FanControl-AutoStart 的两条电池限制
echo ============================================================
echo   schtasks /Create 会把任务写成：
echo     - 用电池时不启动
echo     - 切到电池时停止
echo   笔记本不插电时恰恰更需要风扇控制，所以必须改掉。
echo.
powershell -NoProfile -Command "$n='FanControl-AutoStart'; try { $t=Get-ScheduledTask -TaskName $n -ErrorAction Stop } catch { Write-Host '  [X] 任务不存在，先跑一次 安装并切换.bat'; exit 2 }; $t.Settings.DisallowStartIfOnBatteries=$false; $t.Settings.StopIfGoingOnBatteries=$false; $t.Settings.ExecutionTimeLimit='PT0S'; Set-ScheduledTask -TaskName $n -Settings $t.Settings | Out-Null; $c=Get-ScheduledTask -TaskName $n; Write-Host ''; Write-Host ('  DisallowStartIfOnBatteries = ' + $c.Settings.DisallowStartIfOnBatteries); Write-Host ('  StopIfGoingOnBatteries     = ' + $c.Settings.StopIfGoingOnBatteries); Write-Host ('  ExecutionTimeLimit         = ' + $c.Settings.ExecutionTimeLimit)"
if errorlevel 1 goto fail
echo.
echo   已修复：现在用电池开机也会启动风扇控制。
echo.
pause
exit /b 0

:fail
echo.
echo   [X] 修复失败。若提示任务不存在，请先跑一次 安装并切换.bat。
echo.
pause
exit /b 1
