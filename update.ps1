[CmdletBinding()]
param(
    [switch]$NoLaunch,
    [switch]$SkipPresentMon,
    [switch]$SkipSensors,
    [switch]$SkipAudio,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$installer = Join-Path $tempRoot ("Blob-update-" + [guid]::NewGuid() + '.ps1')

try {
    Write-Host 'Downloading the current Blob installer from GitHub...' -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://raw.githubusercontent.com/GAEONN/Blob/main/install.ps1' `
        -OutFile $installer

    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer, '-Update')
    if ($NoLaunch) { $arguments += '-NoLaunch' }
    if ($SkipPresentMon) { $arguments += '-SkipPresentMon' }
    if ($SkipSensors) { $arguments += '-SkipSensors' }
    if ($SkipAudio) { $arguments += '-SkipAudio' }
    if ($DryRun) { $arguments += '-DryRun' }

    & powershell.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Blob update failed with exit code $LASTEXITCODE."
    }
} finally {
    if (Test-Path -LiteralPath $installer) {
        $resolved = [IO.Path]::GetFullPath($installer)
        if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolved -Force
        }
    }
}
