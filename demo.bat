@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0BinanceCopyMonitor.exe" (
    start "" "%~dp0BinanceCopyMonitor.exe" --demo
    exit /b
)
python -m binance_monitor --demo
if errorlevel 1 pause
