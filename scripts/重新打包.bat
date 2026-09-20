@echo off
rem Rebuild FanControl.exe (requires Python env with pyinstaller)
cd /d "%~dp0"
python -m PyInstaller --onefile --noconsole --uac-admin --name FanControl --icon fan.ico --hidden-import=pystray._win32 --add-binary "LpcACPIEC.bin;." fan_control.py
move /Y "dist\FanControl.exe" "..\FanControl.exe"
rd /s /q build dist __pycache__ 2>nul
echo.
echo Rebuilt: ..\FanControl.exe
pause
