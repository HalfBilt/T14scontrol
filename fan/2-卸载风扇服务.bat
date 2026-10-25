@echo off
rem ---- console codepage adaptive: keep Python output readable ----
for /f "tokens=2 delims=: " %%c in ('chcp') do set "CP=%%c"
if "%CP%"=="936" set "PYTHONIOENCODING=gbk"
if "%CP%"=="65001" set "PYTHONIOENCODING=utf-8"
rem 停用并删除 T14s 智能风扇控制服务(需要管理员)
cd /d "%~dp0"
if "%FCS_ELEV%"=="1" goto run
fltmc >nul 2>&1
if %errorlevel%==0 goto run
set FCS_ELEV=1
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:run
"%~dp0..\runtime\python.exe" "%~dp0src\fan_service_ctl.py" uninstall
pause
