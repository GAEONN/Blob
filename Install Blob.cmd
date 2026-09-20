@echo off
setlocal
title Blob full installer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "blob_exit=%ERRORLEVEL%"
if not "%blob_exit%"=="0" (
    echo.
    echo Blob setup did not finish successfully. Review the message above.
    pause
)
exit /b %blob_exit%
