@echo off
setlocal
title Blob updater
echo Updating Blob to the latest version from GitHub...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/GAEONN/Blob/main/update.ps1'))) %*"
set "blob_exit=%ERRORLEVEL%"
if not "%blob_exit%"=="0" (
    echo.
    echo Blob update did not finish successfully. Review the message above.
    pause
)
exit /b %blob_exit%
