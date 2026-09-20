@echo off
rem Remove logon autostart task (needs admin), self-elevating
cd /d "%~dp0"
reg query "HKU\S-1-5-19" >nul 2>&1
if %errorlevel%==0 goto run
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0remove_autostart.bat' -Verb RunAs"
exit /b
:run
schtasks /Delete /F /TN "FanControl-AutoStart"
echo.
echo Task FanControl-AutoStart removed.
pause
exit /b
