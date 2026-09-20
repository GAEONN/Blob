[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$InstallRoot)

$ErrorActionPreference = 'SilentlyContinue'

function Show-Result([string]$Name, [bool]$Ready, [string]$Detail) {
    $mark = if ($Ready) { '[ready]' } else { '[needs attention]' }
    $color = if ($Ready) { 'Green' } else { 'Yellow' }
    Write-Host ("  {0,-18} {1} {2}" -f $Name, $mark, $Detail) -ForegroundColor $color
}

$python = Join-Path $InstallRoot '.venv\Scripts\pythonw.exe'
$blob = Join-Path $InstallRoot 'blob.pyw'
$presentMon = Join-Path $InstallRoot 'tools\PresentMon-2.5.1-x64.exe'
$sensorExe = Join-Path $InstallRoot 'tools\LibreHardwareMonitor\LibreHardwareMonitor.exe'
$sensorAuth = Join-Path $InstallRoot 'tools\LibreHardwareMonitor\.blob-http-auth.json'

$audioReady = $false
try {
    $audioReady = [bool](Get-PnpDevice -PresentOnly | Where-Object {
        $_.FriendlyName -like 'CABLE Input*VB-Audio*' -or $_.FriendlyName -like 'CABLE Output*VB-Audio*'
    })
} catch { }

$sensorReady = [bool](Get-ScheduledTask -TaskName 'Blob Sensors') -or
               [bool](Get-Process -Name 'LibreHardwareMonitor') -or
               (Test-Path -LiteralPath $sensorExe)
$sensorFeedReady = $false
if (Test-Path -LiteralPath $sensorAuth) {
    try {
        $auth = Get-Content -LiteralPath $sensorAuth -Raw | ConvertFrom-Json
        $pair = '{0}:{1}' -f $auth.username, $auth.password
        $token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
        $feed = Invoke-RestMethod -UseBasicParsing -Uri 'http://127.0.0.1:8085/data.json' `
                                  -Headers @{ Authorization = 'Basic ' + $token } -TimeoutSec 2
        $sensorFeedReady = [bool]$feed
    } catch { }
}

Write-Host 'Installation check:' -ForegroundColor White
Show-Result 'Blob core' ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $blob)) 'private Python environment'
Show-Result 'FPS' (Test-Path -LiteralPath $presentMon) 'PresentMon helper'
Show-Result 'Sensors' $sensorReady 'LibreHardwareMonitor provider'
Show-Result 'Sensor feed' $sensorFeedReady 'authenticated local CPU, GPU, and fan readings'
Show-Result 'Audio' $audioReady ($(if ($audioReady) { 'VB-CABLE endpoints detected' } else { 'restart Windows if the driver was just installed' }))
Write-Host '  Music              [ready] any Windows media session; no Apple Music installation required' -ForegroundColor Green
