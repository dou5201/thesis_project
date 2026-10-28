@echo off
rem Double-click to start the Cantilever Beam DAQ in simulation mode on Windows.
cd /d "%~dp0"
python run_app.py --simulate
if errorlevel 1 (
    echo.
    echo The program stopped with an error. See the message above.
    pause
)
