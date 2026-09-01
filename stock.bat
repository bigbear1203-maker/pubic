@echo off
chcp 65001 >nul
rem  cd to this batch file own folder (%~dp0) so the path is always right.
rem  This is why you can double-click it from anywhere without errors.
cd /d "%~dp0"
python stock.py %*
