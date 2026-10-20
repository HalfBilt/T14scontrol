@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   FanControl - ThinkPad T14s Gen 4 风扇控制
echo ============================================================
echo.

if not exist "C:\Program Files\PawnIO\PawnIOLib.dll" (
  echo [X] 没装 PawnIO 驱动 —— 风扇工具跑不起来。
  echo.
  echo     请先安装: https://pawnio.eu/   ^(下载安装包, 装完可能要重启^)
  echo     装好后 PawnIOLib.dll 应该在 C:\Program Files\PawnIO\ 下。
  echo.
  pause
  exit /b 1
)

fltmc >nul 2>&1
if %errorlevel%==0 goto run
echo 正在请求管理员权限...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:run
if not exist "%~dp0runtime\pythonw.exe" (
  echo [X] 缺少 runtime 目录（捆绑的 Python）。
  pause
  exit /b 1
)
start "" "%~dp0runtime\pythonw.exe" "%~dp0src\fan_control.py"
echo 已启动，托盘里会多一个风扇图标（绿=智能 / 橙=手动 / 红=最强散热 / 蓝=EC自动）。
echo 右键图标可切模式、开/关开机自启、退出。
timeout /t 3 >nul
