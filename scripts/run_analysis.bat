@echo off
cd /d "%~dp0.."
call venv\Scripts\activate

echo Running Market Signal Lab (Pre-market Mode)...
python main.py --mode pre_market

echo.
pause
