@echo off
setlocal
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --distpath . --name BinanceCopyMonitor launcher.py
if errorlevel 1 (
    echo Build failed. Python 3.11+ and PyInstaller are required.
    pause
    exit /b 1
)
echo Built: BinanceCopyMonitor.exe
