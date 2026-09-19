@echo off
REM ASCII only: cmd decodes .bat as GBK and would run mangled CJK as commands.
REM Tray mode + turn the glass layer on immediately. No console window.
cd /d "%~dp0"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
    echo [win-duo] venv not found: %PYW%
    echo [win-duo] run this first:  powershell -ExecutionPolicy Bypass -File tools\setup_env.ps1
    pause
    exit /b 1
)
start "" "%PYW%" "%~dp0main.py" --glass
