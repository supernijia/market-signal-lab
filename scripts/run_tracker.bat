@echo off
cd /d "%~dp0.."
call venv\Scripts\activate

echo Running Strategy Tracker...
python main.py --mode track

echo.
pause
