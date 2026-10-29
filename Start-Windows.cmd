@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 server.py
) else (
    python server.py
)
if errorlevel 1 (
    echo.
    echo The app could not start. Python 3.10 or newer is required.
    echo If Python is installed, read the error above for details.
    pause
)
