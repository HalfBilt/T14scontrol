@echo off
rem ---- console codepage adaptive: keep Python output readable ----
for /f "tokens=2 delims=: " %%c in ('chcp') do set "CP=%%c"
if "%CP%"=="936" set "PYTHONIOENCODING=gbk"
if "%CP%"=="65001" set "PYTHONIOENCODING=utf-8"
rem 查看 T14s 风扇服务状态 + 最近日志(不需要管理员)
cd /d "%~dp0"
"%~dp0..\runtime\python.exe" "%~dp0src\fan_service_ctl.py" status
echo.
if exist "%~dp0fan_service.log" (
  echo ==== fan_service.log 最近 25 行 ====
  powershell -NoProfile -Command "Get-Content -LiteralPath '%~dp0fan_service.log' -Encoding UTF8 -Tail 25"
) else (
  echo (还没有 fan_service.log)
)
echo.
pause
