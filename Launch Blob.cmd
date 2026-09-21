@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Blob needs to finish its local setup first.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
    exit /b %ERRORLEVEL%
)

start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.pyw" %*
