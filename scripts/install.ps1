<#
    Registers DeadlineWatch as a background task that starts when you log in.

    Usage:
        powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
        powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -StartNow
#>
param(
    [string]$TaskName = "DeadlineWatch",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "DeadlineWatch installer" -ForegroundColor Cyan
Write-Host "Working directory: $root`n"

# --- 1. locate Python -----------------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "Python 3.10+ was not found on PATH. Install it from https://python.org and re-run." -ForegroundColor Red
    exit 1
}
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "Python: $python"

# --- 2. create .env -------------------------------------------------------
if (-not (Test-Path (Join-Path $root ".env"))) {
    Copy-Item (Join-Path $root ".env.example") (Join-Path $root ".env")
    Write-Host "Created .env - fill in your credentials next." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists, leaving it untouched."
}

# --- 3. register the scheduled task ---------------------------------------
$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "-m src.main watch --dashboard" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$taskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $taskSettings `
        -Description "DeadlineWatch - scans Gmail for deadlines and alerts you on Telegram" `
        -Force | Out-Null
    Write-Host "Registered scheduled task '$TaskName' (starts at logon)." -ForegroundColor Green
}
catch {
    Write-Host "Could not register the scheduled task ($($_.Exception.Message))." -ForegroundColor Yellow
    Write-Host "Falling back to the Startup folder..."
    $startup = [Environment]::GetFolderPath("Startup")
    $shortcut = Join-Path $startup "DeadlineWatch.cmd"
    @"
@echo off
cd /d "$root"
"$pythonw" -m src.main watch --dashboard
"@ | Set-Content -Path $shortcut -Encoding ASCII
    Write-Host "Created $shortcut" -ForegroundColor Green
}

# --- 4. next steps --------------------------------------------------------
Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  1. Fill in .env  (Gmail app password, Telegram token, LLM key)"
Write-Host "  2. python -m src.main check         # verify everything works"
Write-Host "  3. python -m src.main test-telegram # grab your Telegram chat id"
Write-Host "  4. python -m src.main watch         # run it in the foreground"

if ($StartNow) {
    Write-Host ""
    Write-Host "Starting the task now..."
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Running. Follow the log at .\data\deadlinewatch.log" -ForegroundColor Green
}
