@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0BinanceCopyMonitor.exe" (
    start "" "%~dp0BinanceCopyMonitor.exe"
    exit /b
)
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw -m binance_monitor
    exit /b
)
where python >nul 2>nul
if %errorlevel% equ 0 (
    python -m binance_monitor
    if errorlevel 1 pause
    exit /b
)
echo Python 3.11 or newer is required. Install Python with Tcl/Tk support.
pause
