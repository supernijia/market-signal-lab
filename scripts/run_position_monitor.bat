@echo off
cd /d "%~dp0\.."
echo Starting Real-Time Position Monitor...
python monitor.py
pause
