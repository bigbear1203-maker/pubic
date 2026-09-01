@echo off
chcp 65001 >nul
cd /d "%~dp0"
python stock.py check
echo.
echo 按任意鍵關閉視窗。
pause >nul
