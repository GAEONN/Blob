[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [string]$TargetUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name,
    [switch]$SkipSensors,
    [switch]$SkipAudio,
    [switch]$RepairSensorEndpoint,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$LhmVersion = '0.9.6'
$LhmUrl = 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip'
$LhmSha256 = '086D9F1B5A99E643EDC2CFAAAC16051685B551E4C5AC0B32A57C58C0E529C001'
$VbCableUrl = 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip'
$VbCableSha256 = 'B950E39F01AF1D04EA623C8F6D8EB9B6EA5C477C637295FABF20631C85116BFB'
$SensorTaskName = 'Blob Sensors'

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Argument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-ElevatedSelf {
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Quote-Argument $PSCommandPath),
              '-InstallRoot', (Quote-Argument $InstallRoot), '-TargetUser', (Quote-Argument $TargetUser))
    if ($SkipSensors) { $args += '-SkipSensors' }
    if ($SkipAudio) { $args += '-SkipAudio' }
    if ($RepairSensorEndpoint) { $args += '-RepairSensorEndpoint' }
    try {
        $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $args -Wait -PassThru
        exit $process.ExitCode
    } catch {
        Write-Warning 'Administrator approval was cancelled. Sensors and audio were not changed.'
        exit 1
    }
}

function New-SafeTempDirectory([string]$Purpose) {
    $path = Join-Path ([IO.Path]::GetTempPath()) ("Blob-$Purpose-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $path | Out-Null
    return $path
}

function Remove-SafeTempDirectory([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    $resolved = [IO.Path]::GetFullPath($Path)
    $tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolved.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a non-temporary directory: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Get-VerifiedDownload([string]$Uri, [string]$Destination, [string]$ExpectedSha256) {
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Destination
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
    if ($actual -ne $ExpectedSha256) {
        throw "Downloaded file hash mismatch. Expected $ExpectedSha256 but received $actual."
    }
}

function Set-LhmConfiguration([string]$Executable, [switch]$RotateCredential) {
    $configPath = [IO.Path]::ChangeExtension($Executable, '.config')
    $authPath = Join-Path (Split-Path -Parent $Executable) '.blob-http-auth.json'
    $auth = $null
    if (-not $RotateCredential -and (Test-Path -LiteralPath $authPath)) {
        try { $auth = Get-Content -LiteralPath $authPath -Raw | ConvertFrom-Json } catch { $auth = $null }
    }
    if (-not $auth -or -not $auth.username -or -not $auth.password) {
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $auth = [pscustomobject]@{
            username = 'blob'
            password = [Convert]::ToBase64String($bytes)
            port = 8085
        }
        $auth | ConvertTo-Json | Set-Content -LiteralPath $authPath -Encoding UTF8
    }
    $doc = New-Object System.Xml.XmlDocument
    if (Test-Path -LiteralPath $configPath) {
        try { $doc.Load($configPath) } catch { $doc.RemoveAll() }
    }
    if (-not $doc.DocumentElement) {
        $null = $doc.AppendChild($doc.CreateXmlDeclaration('1.0', 'utf-8', $null))
        $configuration = $doc.CreateElement('configuration')
        $null = $doc.AppendChild($configuration)
        $null = $configuration.AppendChild($doc.CreateElement('appSettings'))
    }
    $appSettings = $doc.SelectSingleNode('/configuration/appSettings')
    if (-not $appSettings) {
        $appSettings = $doc.CreateElement('appSettings')
        $null = $doc.DocumentElement.AppendChild($appSettings)
    }
    foreach ($pair in @(
        @('startMinMenuItem', 'true'),
        @('minTrayMenuItem', 'true'),
        @('minCloseMenuItem', 'true'),
        # LHM 0.9.5+ can fail to publish its WMI namespace. Blob uses the official
        # JSON API as a fallback, protected with a per-install random credential.
        # LHM hashes this plain configuration value itself when it starts.
        @('runWebServerMenuItem', 'true'),
        @('listenerIp', '?'),
        @('listenerPort', '8085'),
        @('authenticationEnabled', 'true'),
        @('authenticationUserName', [string]$auth.username),
        @('authenticationPassword', [string]$auth.password)
    )) {
        $node = $appSettings.SelectSingleNode("add[@key='$($pair[0])']")
        if (-not $node) {
            $node = $doc.CreateElement('add')
            $null = $appSettings.AppendChild($node)
        }
        $node.SetAttribute('key', $pair[0])
        $node.SetAttribute('value', $pair[1])
    }
    $doc.Save($configPath)
}

function Find-ExistingLhm {
    $running = Get-Process -Name 'LibreHardwareMonitor' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($running) {
        try {
            if ($running.Path -and (Test-Path -LiteralPath $running.Path)) { return $running.Path }
        } catch { }
    }
    foreach ($candidate in @(
        (Join-Path $InstallRoot 'tools\LibreHardwareMonitor\LibreHardwareMonitor.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\LibreHardwareMonitor.exe'),
        (Join-Path $env:ProgramFiles 'WinGet\Links\LibreHardwareMonitor.exe')
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Install-SensorSupport {
    Write-Host 'Installing universal CPU, GPU, motherboard, and fan sensor support...'
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'WinGet is required to install the signed PawnIO sensor driver.'
    }

    & winget.exe install --id namazso.PawnIO --exact --silent --disable-interactivity `
        --accept-package-agreements --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "PawnIO installation failed with exit code $LASTEXITCODE."
    }

    $lhmExe = Find-ExistingLhm
    if (-not $lhmExe) {
        $destination = Join-Path $InstallRoot 'tools\LibreHardwareMonitor'
        $tempRoot = New-SafeTempDirectory 'sensors'
        try {
            $archive = Join-Path $tempRoot 'LibreHardwareMonitor.zip'
            $expanded = Join-Path $tempRoot 'expanded'
            Get-VerifiedDownload $LhmUrl $archive $LhmSha256
            Expand-Archive -LiteralPath $archive -DestinationPath $expanded
            New-Item -ItemType Directory -Force -Path $destination | Out-Null
            Copy-Item -Path (Join-Path $expanded '*') -Destination $destination -Recurse -Force
            Set-Content -LiteralPath (Join-Path $destination '.blob-version') -Value $LhmVersion -Encoding ASCII
            $lhmExe = Join-Path $destination 'LibreHardwareMonitor.exe'
        } finally {
            Remove-SafeTempDirectory $tempRoot
        }
    }
    if (-not (Test-Path -LiteralPath $lhmExe)) {
        throw 'LibreHardwareMonitor was downloaded but its executable is missing.'
    }

    Set-LhmConfiguration $lhmExe
    $action = New-ScheduledTaskAction -Execute $lhmExe -WorkingDirectory (Split-Path -Parent $lhmExe)
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
    $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $SensorTaskName -Action $action -Trigger $trigger -Principal $principal `
        -Description 'Starts the read-only hardware sensor provider used by Blob.' -Force | Out-Null
    if (-not (Get-Process -Name 'LibreHardwareMonitor' -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName $SensorTaskName
    }
    Write-Host 'Sensor support is installed and starts minimized with Windows.' -ForegroundColor Green
}

function Test-VbCable {
    try {
        $pnp = Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object {
            $_.FriendlyName -like 'CABLE Input*VB-Audio*' -or $_.FriendlyName -like 'CABLE Output*VB-Audio*'
        }
        if ($pnp) { return $true }
    } catch { }
    try {
        return [bool](Get-CimInstance Win32_SoundDevice -ErrorAction Stop | Where-Object {
            $_.Name -like '*VB-Audio Virtual Cable*'
        })
    } catch { return $false }
}

function Install-AudioSupport {
    if (Test-VbCable) {
        Write-Host 'VB-CABLE audio support is already installed.' -ForegroundColor Green
        return
    }

    Write-Host ''
    Write-Host 'Preparing system-wide audio support...' -ForegroundColor White
    Write-Host 'VB-CABLE is third-party donationware from VB-Audio.'
    Write-Host 'Its original signed setup window will open. Select Install Driver; a restart is required afterward.'
    $tempRoot = New-SafeTempDirectory 'audio'
    try {
        $archive = Join-Path $tempRoot 'VBCABLE_Driver_Pack45.zip'
        $expanded = Join-Path $tempRoot 'expanded'
        Get-VerifiedDownload $VbCableUrl $archive $VbCableSha256
        Expand-Archive -LiteralPath $archive -DestinationPath $expanded
        $setup = Join-Path $expanded 'VBCABLE_Setup_x64.exe'
        if (-not (Test-Path -LiteralPath $setup)) { throw 'The VB-CABLE setup executable is missing.' }
        $signature = Get-AuthenticodeSignature -LiteralPath $setup
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*BUREL VINCENT*') {
            throw 'The VB-CABLE installer does not have the expected valid publisher signature.'
        }
        $process = Start-Process -FilePath $setup -WorkingDirectory $expanded -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            Write-Warning "VB-CABLE setup returned exit code $($process.ExitCode)."
        }
    } finally {
        Remove-SafeTempDirectory $tempRoot
    }

    if (Test-VbCable) {
        Write-Host 'VB-CABLE is installed. Restart Windows before using Blob Sound.' -ForegroundColor Green
    } else {
        Write-Warning 'VB-CABLE is not visible yet. If installation succeeded, restart Windows and check again.'
    }
}

if ($DryRun) {
    if (-not $SkipSensors) {
        Write-Host "PLAN sensors: PawnIO via WinGet; verified LibreHardwareMonitor $LhmVersion; elevated startup task for $TargetUser"
    }
    if (-not $SkipAudio) {
        Write-Host 'PLAN audio: verified official VB-CABLE package; validate publisher; open original driver setup'
    }
    exit 0
}

if ($RepairSensorEndpoint) {
    if (-not (Test-Administrator)) { Invoke-ElevatedSelf }
    $lhmExe = Find-ExistingLhm
    if (-not $lhmExe) { throw 'LibreHardwareMonitor is not installed.' }
    Stop-ScheduledTask -TaskName $SensorTaskName -ErrorAction SilentlyContinue
    Get-Process -Name 'LibreHardwareMonitor' -ErrorAction SilentlyContinue | Stop-Process -Force
    Set-LhmConfiguration $lhmExe -RotateCredential
    Start-ScheduledTask -TaskName $SensorTaskName
    Write-Host 'Blob sensor endpoint repaired and restarted.' -ForegroundColor Green
    exit 0
}

if (-not (Test-Administrator)) { Invoke-ElevatedSelf }

$failed = $false
if (-not $SkipSensors) {
    try { Install-SensorSupport } catch {
        $failed = $true
        Write-Warning "Sensor setup failed: $($_.Exception.Message)"
    }
}
if (-not $SkipAudio) {
    try { Install-AudioSupport } catch {
        $failed = $true
        Write-Warning "Audio setup failed: $($_.Exception.Message)"
    }
}

if ($failed) { exit 1 }
exit 0
