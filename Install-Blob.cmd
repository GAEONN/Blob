@echo off
setlocal
title Blob installer
rem Inside an extracted checkout, use its installer; otherwise download the current installer from main.
if exist "%~dp0install.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
) else (
    echo Downloading the latest Blob from GitHub...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/GAEONN/Blob/main/install.ps1'))) %*"
)
set "blob_exit=%ERRORLEVEL%"
if not "%blob_exit%"=="0" (
    echo.
    echo Blob setup did not finish successfully. Review the message above.
    pause
)
exit /b %blob_exit%
