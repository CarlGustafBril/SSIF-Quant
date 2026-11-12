# Registers and starts the "PolyRecorder" scheduled task for the current user.
#   - starts at logon, and is re-checked every 5 minutes: if the recorder is not running it is started again
#   - runs with no window at all (conhost --headless), so there is nothing on screen to close by accident
#   - never runs twice (a second copy exits by itself)
# Run from PowerShell:   .\recorder\windows\install_task.ps1
# Remove with:           Unregister-ScheduledTask -TaskName PolyRecorder -Confirm:$false

$script = Join-Path $PSScriptRoot "run_recorder.cmd"
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless cmd.exe /c `"$script`""
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$every5min = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "PolyRecorder" -Action $action -Trigger $atLogon, $every5min -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "PolyRecorder"
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName "PolyRecorder" | Select-Object TaskName, State
Get-Process python -ErrorAction SilentlyContinue | Select-Object Name, Id, StartTime
