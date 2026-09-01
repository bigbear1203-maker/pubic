@echo off
chcp 65001 >nul
cd /d "%~dp0"
python stock.py report
echo.
echo Press any key to close.
pause >nul
