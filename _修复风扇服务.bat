@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
title 修复风扇服务（标记删除状态）

set "ROOT=%APPDATA%\T14scontrol"
if not exist "%ROOT%\runtime\python.exe" (
  echo [X] 找不到 runtime\python.exe
  echo     应在 %APPDATA%\T14scontrol\runtime\
  pause
  exit /b 1
)
set "PY=%ROOT%\runtime\python.exe"
set "CTL=%ROOT%\fan\src\fan_service_ctl.py"

net session >nul 2>&1
if errorlevel 1 (
  echo 正在请求管理员权限，请在弹窗中点是...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ==========================================
echo   修复风扇服务
echo ==========================================
echo.

echo [1/5] 停止服务
sc stop T14sFanSvc >nul 2>&1
echo       等待进程完全退出（8 秒）...
timeout /t 8 /nobreak >nul

echo [2/5] 检查删除标记
reg query "HKLM\SYSTEM\CurrentControlSet\Services\T14sFanSvc" /v DeleteFlag >nul 2>&1
if errorlevel 1 (
  echo       无删除标记，服务已彻底移除
  goto reinstall
)
echo       仍有 DeleteFlag，再删一次
sc delete T14sFanSvc >nul 2>&1
timeout /t 3 /nobreak >nul

echo [3/5] 二次检查
reg query "HKLM\SYSTEM\CurrentControlSet\Services\T14sFanSvc" /v DeleteFlag >nul 2>&1
if errorlevel 1 (
  echo       已清除
  goto reinstall
)
echo.
echo ==========================================
echo   [!] 服务仍处于标记删除状态
echo ==========================================
echo   进程没完全退出时，Windows 不允许重建同名服务。
echo   解决办法：重启电脑，再运行 1-安装风扇服务.bat
echo.
pause
exit /b 1

:reinstall
echo [4/5] 重新注册服务
"%PY%" "%CTL%" install
echo.
echo [5/5] 验证
"%PY%" "%CTL%" status
echo.
pause
