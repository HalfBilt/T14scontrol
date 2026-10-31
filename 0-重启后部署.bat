@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
title 重启后部署

set "ROOT=%APPDATA%\T14scontrol"
if not exist "%ROOT%\runtime\python.exe" (
  echo [X] 找不到 runtime\python.exe
  pause
  exit /b 1
)
set "PY=%ROOT%\runtime\python.exe"
set "PYW=%ROOT%\runtime\pythonw.exe"

net session >nul 2>&1
if errorlevel 1 (
  echo 正在请求管理员权限，请在弹窗中点是...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ==========================================
echo   重启后部署
echo ==========================================
echo.

echo [1/4] 安装风扇服务
"%PY%" "%ROOT%\fan\src\fan_service_ctl.py" install
echo.

echo [2/4] 确认 CPU 服务与计划任务
"%PY%" "%ROOT%\cpu\CpuPLFix.py" install
echo.

echo [3/4] 启动任务栏显示
taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq T14scontrolHUD" >nul 2>&1
start "" "%PYW%" "%ROOT%\hud\hud.py"
timeout /t 2 /nobreak >nul
echo.

echo [4/4] 验证
echo ---- 服务状态 ----
sc query CpuPLFixSvc | findstr /i "STATE"
sc query T14sFanSvc  | findstr /i "STATE"
sc query TSCtl       | findstr /i "STATE"
echo.
echo ---- 计划任务 ----
schtasks /query /tn CpuPLFix >nul 2>&1 && (echo CpuPLFix  已存在) || (echo CpuPLFix  缺失 ^(不影响使用, 服务本身会修^))
schtasks /query /tn T14sHUDGuard >nul 2>&1 && (echo HUDGuard  已存在) || (echo HUDGuard  缺失)
echo.
echo ---- 日志位置 ----
if exist "%ROOT%\cpu\logs\fix.log" echo   cpu\logs\fix.log
if exist "%ROOT%\fan\logs\fan_service.log" echo   fan\logs\fan_service.log
if exist "%ROOT%\hud\logs\toggle.log" echo   hud\logs\toggle.log
echo.
echo ==========================================
echo   完成。三个服务应全部 RUNNING。
echo ==========================================
pause
