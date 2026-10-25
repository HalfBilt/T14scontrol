@echo off
rem ============================================================
rem  EC fix - single entry point (2026-10-23).
rem  Driver relocation to System32 + service repoint + EC fixes.
rem
rem  Lives in <repo>\ec-fix\ but finds the LIVE install on its own
rem  (via %APPDATA%\T14scontrol), so it can be run from either place.
rem
rem  ASCII only. MUST keep CRLF line endings -- cmd.exe cannot resolve
rem  ":label" / "goto" in an LF-only .bat.
rem ============================================================
for /f "tokens=2 delims=: " %%c in ('chcp') do set "CP=%%c"
if "%CP%"=="936" set "PYTHONIOENCODING=gbk"
if "%CP%"=="65001" set "PYTHONIOENCODING=utf-8"

rem ---- locate the live install --------------------------------------
set "ROOT=%APPDATA%\T14scontrol"
if not exist "%ROOT%\cpu\CpuPLFix.py" set "ROOT=%~dp0..\.."
cd /d "%ROOT%" 2>nul
if errorlevel 1 goto noroot
set "ROOT=%CD%"
if not exist "%ROOT%\cpu\CpuPLFix.py" goto noroot

if "%ECFIX_ELEV%"=="1" goto run
fltmc >nul 2>&1
if %errorlevel%==0 goto run

set ECFIX_ELEV=1
echo Requesting administrator privileges ...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
if errorlevel 1 (
  echo.
  echo  [X] Elevation was cancelled or failed. Nothing was changed.
  echo.
  pause
)
exit /b

:run
title EC fix
echo ============================================================
echo  install root = %ROOT%
echo  [1/4] clearing __pycache__
if exist "%ROOT%\cpu\__pycache__" rd /s /q "%ROOT%\cpu\__pycache__"
if exist "%ROOT%\fan\src\__pycache__" rd /s /q "%ROOT%\fan\src\__pycache__"
echo        ok

echo  [2/4] syntax check
if not exist "%ROOT%\runtime\python.exe" (
  echo   [X] %ROOT%\runtime\python.exe missing
  goto bad
)
"%ROOT%\runtime\python.exe" -m py_compile "%ROOT%\cpu\CpuPLFix.py"
if errorlevel 1 goto bad
"%ROOT%\runtime\python.exe" -m py_compile "%ROOT%\fan\src\fan_control.py"
if errorlevel 1 goto bad
echo        ok

echo  [3/4] applying L2 ^(driver relocation + service repoint^)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply-l2.ps1"
echo        exit code = %errorlevel%

echo  [4/4] result log
echo ------------------------------------------------------------
if exist "%ROOT%\logs\apply-l2.log" (
  powershell -NoProfile -Command "Get-Content -Encoding UTF8 '%ROOT%\logs\apply-l2.log'"
) else (
  echo   no log found - the L2 script did not start
)
echo ------------------------------------------------------------
echo.
echo What to expect:
echo   - TSCtl driver in C:\Windows\System32\drivers
echo   - both services RUNNING, fan still spinning
echo   - on any failure the driver path is rolled back automatically
echo.
echo After the next reboot, APPDATA\CpuPLFix can be deleted ^(stale^).
echo.
pause
exit /b 0

:noroot
echo.
echo  [X] cannot find the T14scontrol install.
echo      Looked in: %APPDATA%\T14scontrol and %~dp0..\..
echo      Both must contain cpu\CpuPLFix.py
echo.
pause
exit /b 1

:bad
echo.
echo  [X] pre-check FAILED - nothing was changed, services untouched.
pause
exit /b 1
