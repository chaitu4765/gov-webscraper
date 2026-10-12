$ErrorActionPreference = 'Stop'
$taskName = 'TenderDeskCollector'
$appPath = Join-Path $PSScriptRoot 'app.py'
if (-not (Test-Path -LiteralPath $appPath)) { throw 'app.py is missing.' }
$pythonPath = (Get-Command pythonw.exe -ErrorAction Stop).Source
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
    $existingActions = @($existingTask.Actions)
    if ($existingActions.Count -ne 1 -or $existingActions[0].Arguments -ne ('"' + $appPath + '"')) {
        throw 'An unrelated task with this name exists; it was not modified.'
    }
}
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $appPath + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings -Description 'TenderDesk local dashboard and periodic public tender collector. Runs while this user is signed in.' -RunLevel Limited -Force | Select-Object TaskName,State
