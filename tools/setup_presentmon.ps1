$ErrorActionPreference = 'Stop'
# Official Intel/GameTechDev release, pinned and hash-verified. No installer,
# admin elevation, PATH changes, or service installation.
$binary = Join-Path $PSScriptRoot 'PresentMon-2.5.1-x64.exe'
$expected = '9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191'
if ((Test-Path -LiteralPath $binary) -and ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLower() -eq $expected)) {
    Write-Output 'PresentMon is already installed and verified.'
    exit 0
}
$download = Join-Path $PSScriptRoot 'PresentMon-download.tmp'
Invoke-WebRequest -Uri 'https://github.com/GameTechDev/PresentMon/releases/download/v2.5.1/PresentMon-2.5.1-x64.exe' -OutFile $download
if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLower() -ne $expected) {
    throw 'PresentMon hash mismatch. The download has NOT been installed or executed.'
}
Move-Item -LiteralPath $download -Destination $binary -Force
Write-Output 'PresentMon installed and verified. Open Gaming in Blob to start capture.'
