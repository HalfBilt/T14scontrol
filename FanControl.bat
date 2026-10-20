@echo off
rem 开机自启用的静默启动器（计划任务 FanControl-AutoStart 指向这个文件）
cd /d "%~dp0"
start "" "%~dp0runtime\pythonw.exe" "%~dp0src\fan_control.py"
