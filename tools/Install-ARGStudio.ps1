# Run once from PowerShell as the current Windows user. No admin access required.
[CmdletBinding()]
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$TaskName = 'MoneyPrinterTurbo ARG Studio'
if ($Uninstall) {
    $Root = Split-Path -Parent $PSScriptRoot
    & (Join-Path $Root '.venv\Scripts\pythonw.exe') (Join-Path $PSScriptRoot 'argstudio_windows.py') --stop
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Automatic startup removed. No application data was deleted.'
    exit
}
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonWindowless = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
$Supervisor = Join-Path $PSScriptRoot 'argstudio_windows.py'
if (-not (Test-Path $PythonWindowless)) { throw 'Run uv sync --frozen --python 3.11 first.' }
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $PythonWindowless -Argument ('"{0}"' -f $Supervisor) -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output 'Installed: launches at sign-in with no console. Open http://127.0.0.1:8501'
