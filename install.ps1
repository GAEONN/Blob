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

function Write-Step([int]$Number, [string]$Message) {
    Write-Host "[$Number/5] $Message" -ForegroundColor Cyan
}

function Find-Python {
    foreach ($version in @('3.13', '3.12', '3.11', '3.10')) {
        try {
            $path = & py "-$version" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $path)) {
                return $path.Trim()
            }
        } catch { }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Python 3.10+ is required. Install Python from python.org, then run this command again.'
    }
    Write-Host 'Python was not found. Installing Python 3.13 with WinGet...'
    & winget install --id Python.Python.3.13 --exact --silent --disable-interactivity `
        --accept-package-agreements --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Python installation failed with exit code $LASTEXITCODE."
    }
    $installed = Join-Path $env:LocalAppData 'Programs\Python\Python313\python.exe'
    if (Test-Path -LiteralPath $installed) { return $installed }
    return (Find-Python)
}

Write-Host ''
Write-Host 'Blob full installer' -ForegroundColor White
Write-Host 'Core app + FPS + universal sensors + system-wide audio' -ForegroundColor DarkGray
Write-Host ''

$localCheckout = $PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'blob.pyw'))
$temporaryDownload = $null
if ($localCheckout) {
    $installRoot = $PSScriptRoot
} else {
    $installRoot = Join-Path $env:LocalAppData 'Programs\Blob'
}

if ($DryRun) {
    Write-Step 1 "Install or update Blob at $installRoot"
    Write-Step 2 'Create an isolated Python environment and install dependencies'
    Write-Step 3 ($(if ($SkipPresentMon) { 'Skip FPS helper' } else { 'Install the verified PresentMon FPS helper' }))
    $systemParts = @()
    if (-not $SkipSensors) { $systemParts += 'LibreHardwareMonitor sensors' }
    if (-not $SkipAudio) { $systemParts += 'VB-CABLE audio' }
    Write-Step 4 ($(if ($systemParts.Count) { 'Set up ' + ($systemParts -join ' and ') + ' with one UAC prompt' } else { 'Skip optional system components' }))
    Write-Step 5 'Create shortcuts, verify the installation, and launch Blob'
    exit 0
}

if (-not $localCheckout) {
    Write-Step 1 'Downloading Blob...'
    $temporaryDownload = Join-Path ([IO.Path]::GetTempPath()) ("Blob-install-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $temporaryDownload | Out-Null
    $archive = Join-Path $temporaryDownload 'Blob.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/alonsoglunac-debug/Blob/archive/refs/heads/main.zip' -OutFile $archive
    Expand-Archive -LiteralPath $archive -DestinationPath $temporaryDownload
    $sourceRoot = Join-Path $temporaryDownload 'Blob-main'
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
    Copy-Item -Path (Join-Path $sourceRoot '*') -Destination $installRoot -Recurse -Force
} else {
    Write-Step 1 "Using local checkout: $installRoot"
}

try {
    Write-Step 2 'Preparing the private Python environment...'
    $python = Find-Python
    if (-not $python) { $python = Install-Python }
    if (-not $python) { throw 'Python installed, but its executable could not be located.' }

    $venv = Join-Path $installRoot '.venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Blob Python environment.' }
    }
    & $venvPython -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Could not update pip.' }
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $installRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Blob dependencies.' }

    Write-Step 3 ($(if ($SkipPresentMon) { 'Skipping the FPS helper.' } else { 'Installing the verified FPS helper...' }))
    if (-not $SkipPresentMon) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $installRoot 'tools\setup_presentmon.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'PresentMon setup failed.' }
    }

    Write-Step 4 'Preparing system integrations...'
    if (-not $SkipSensors -or -not $SkipAudio) {
        $systemScript = Join-Path $installRoot 'tools\setup_system.ps1'
        $systemArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $systemScript,
                        '-InstallRoot', $installRoot,
                        '-TargetUser', [Security.Principal.WindowsIdentity]::GetCurrent().Name)
        if ($SkipSensors) { $systemArgs += '-SkipSensors' }
        if ($SkipAudio) { $systemArgs += '-SkipAudio' }
        & powershell.exe @systemArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'One or more optional integrations could not be installed. Blob itself is ready.'
        }
    } else {
        Write-Host 'Optional system components were skipped.'
    }

    Write-Step 5 'Creating shortcuts and checking the installation...'
    & $venvPython (Join-Path $installRoot 'make_shortcut.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Blob shortcut.' }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $installRoot 'tools\verify_install.ps1') `
        -InstallRoot $installRoot

    Write-Host ''
    Write-Host "Blob is installed in $installRoot" -ForegroundColor Green
    Write-Host 'Music controls work with Spotify, YouTube, browsers, Apple Music, and other Windows media sessions.'

    if (-not $NoLaunch) {
        $pythonw = Join-Path $venv 'Scripts\pythonw.exe'
        Start-Process -FilePath $pythonw -ArgumentList ('"' + (Join-Path $installRoot 'blob.pyw') + '"') `
            -WorkingDirectory $installRoot -WindowStyle Hidden
    }
} finally {
    if ($temporaryDownload -and (Test-Path -LiteralPath $temporaryDownload)) {
        $resolvedTemp = [IO.Path]::GetFullPath($temporaryDownload)
        $tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if ($resolvedTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
        }
    }
}
