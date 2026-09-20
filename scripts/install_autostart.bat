@echo off
rem Register logon autostart task for FanControl.exe (needs admin), self-elevating
reg query "HKU\S-1-5-19" >nul 2>&1
if %errorlevel%==0 goto run
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:run
set "EXE=%~dp0..\FanControl.exe"
schtasks /Create /F /TN "FanControl-AutoStart" /TR "\"%EXE%\"" /SC ONLOGON /RL HIGHEST /DELAY 0000:15
echo.
echo Task FanControl-AutoStart registered. FanControl.exe starts 15s after logon.
pause
exit /b
