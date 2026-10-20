@echo off
rem 重启 T14s 风扇服务(需要管理员)
cd /d "%~dp0"
if "%FCS_ELEV%"=="1" goto run
fltmc >nul 2>&1
if %errorlevel%==0 goto run
set FCS_ELEV=1
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:run
"%~dp0runtime\python.exe" "%~dp0src\fan_service_ctl.py" stop
timeout /t 2 /nobreak >nul
"%~dp0runtime\python.exe" "%~dp0src\fan_service_ctl.py" start
"%~dp0runtime\python.exe" "%~dp0src\fan_service_ctl.py" status
pause
