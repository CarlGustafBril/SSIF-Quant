# Registers and starts the "PolyRecorder_eth" scheduled task: the same recorder for the ETH contracts,
# writing to C:\polydata_eth. It does not touch the BTC task "PolyRecorder".
# Run from PowerShell:   .\recorder\windows\install_task_eth.ps1
# Remove with:           Unregister-ScheduledTask -TaskName PolyRecorder_eth -Confirm:$false
#                        (then end its python process; delete C:\polydata_eth if the data is not wanted)

$script = Join-Path $PSScriptRoot "run_recorder_eth.cmd"
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless cmd.exe /c `"$script`""
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$every5min = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "PolyRecorder_eth" -Action $action -Trigger $atLogon, $every5min -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "PolyRecorder_eth"
Start-Sleep -Seconds 8
Get-ScheduledTask -TaskName "PolyRecorder", "PolyRecorder_eth" | Select-Object TaskName, State
