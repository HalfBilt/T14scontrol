@echo off
rem ---- console codepage adaptive: keep Python output readable ----
for /f "tokens=2 delims=: " %%c in ('chcp') do set "CP=%%c"
if "%CP%"=="936" set "PYTHONIOENCODING=gbk"
if "%CP%"=="65001" set "PYTHONIOENCODING=utf-8"
rem ============================================================
rem  T14s 智能风扇控制 —— 安装为 Windows 服务(SYSTEM 常驻)
rem  不依赖用户登录; 睡眠唤醒后 1 秒内接管风扇档位
rem ============================================================
cd /d "%~dp0"
if "%FCS_ELEV%"=="1" goto run
fltmc >nul 2>&1
if %errorlevel%==0 goto run
set FCS_ELEV=1
echo Requesting administrator privileges...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:run
if exist "%~dp0..\runtime\python.exe" goto ctl
echo [X] runtime not found - expecting ..\runtime\python.exe (shared runtime).
pause
exit /b 1
:ctl
"%~dp0..\runtime\python.exe" "%~dp0src\fan_service_ctl.py" install
echo.
"%~dp0..\runtime\python.exe" "%~dp0src\fan_service_ctl.py" status
echo.
echo 日志: %~dp0fan_service.log
pause
