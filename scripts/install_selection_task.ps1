param([switch]$RunNow)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$root = Split-Path $PSScriptRoot -Parent
$policyPath = Join-Path $root 'configs\selection_lake_policy.json'
$policy = Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
$python = 'C:\Program Files\Python310\python.exe'
$runner = Join-Path $PSScriptRoot 'update_selection_lake.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Python unavailable: $python" }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'Set an explicit Beijing-time schedule before installing on a different time zone.' }
$arguments = '-X utf8 "' + $runner + '"'
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
$triggers = @($policy.run_times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -RunOnlyIfNetworkAvailable
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Selection-only lake: 19:30 update and 22:30 retry; excludes retired datasets and all derivatives.'
Register-ScheduledTask -TaskName $policy.task_name -InputObject $task -Force | Out-Null
if ($RunNow) { Start-ScheduledTask -TaskName $policy.task_name }
$installed = Get-ScheduledTask -TaskName $policy.task_name
$info = Get-ScheduledTaskInfo -TaskName $policy.task_name
$record = [ordered]@{ task_name=$installed.TaskName; state=[string]$installed.State; user=$installed.Principal.UserId; next_run=$info.NextRunTime.ToString('o'); last_result=$info.LastTaskResult; python=$python; arguments=$arguments; times=$policy.run_times; timezone=(Get-TimeZone).Id }
$directory = Join-Path $root 'logs\selection_update'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $directory 'task_registration.json') -Encoding UTF8
$record | ConvertTo-Json -Depth 5
