@echo off
rem Silent launcher used by the logon task FanControl-AutoStart.
rem Keep this file ASCII-only: cmd.exe parses .bat in the OEM code page.
cd /d "%~dp0"
start "" "%~dp0runtime\pythonw.exe" "%~dp0src\fan_control.py"
exit /b 0
