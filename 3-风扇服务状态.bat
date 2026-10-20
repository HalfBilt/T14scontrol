@echo off
rem 查看 T14s 风扇服务状态 + 最近日志(不需要管理员)
cd /d "%~dp0"
"%~dp0..\runtime\python.exe" "%~dp0src\fan_service_ctl.py" status
echo.
if exist "%~dp0fan_service.log" (
  echo ==== fan_service.log 最近 25 行 ====
  powershell -NoProfile -Command "Get-Content -LiteralPath '%~dp0fan_service.log' -Tail 25"
) else (
  echo (还没有 fan_service.log)
)
echo.
if exist "%~dp0fan_control.log" (
  echo ==== fan_control.log(旧托盘)最近 10 行 ====
  powershell -NoProfile -Command "Get-Content -LiteralPath '%~dp0fan_control.log' -Tail 10"
)
pause
