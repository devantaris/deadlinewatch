<#
    Removes the DeadlineWatch background task and the Startup-folder fallback.
#>
param([string]$TaskName = "DeadlineWatch")

$ErrorActionPreference = "SilentlyContinue"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."

$startup = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startup "DeadlineWatch.cmd"
if (Test-Path $shortcut) {
    Remove-Item $shortcut
    Write-Host "Removed $shortcut"
}

Write-Host "Your .env and data folder were left in place. Delete them manually if you want a clean slate."
