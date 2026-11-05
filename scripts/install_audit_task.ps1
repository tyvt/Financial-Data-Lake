param([switch]$RunNow)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$root = Split-Path $PSScriptRoot -Parent
$policyPath = Join-Path $root 'configs\selection_lake_policy.json'
$policy = Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $policy.weekly_audit_dow -or -not $policy.weekly_audit_task_name) { throw 'Policy is missing weekly audit settings' }
$python = 'C:\Program Files\Python310\python.exe'
$runner = Join-Path $PSScriptRoot 'audit_lake_public.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Python unavailable: $python" }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'Set an explicit Beijing-time schedule before installing on a different time zone.' }
# Working directory is the repo root: the script resolves every path from
# __file__, and the audit reads fixtures under 分析产出, so running it from that
# subdirectory made the scheduled action resolve the runner relative to itself.
$action = New-ScheduledTaskAction -Execute $python -Argument ('-X utf8 "' + $runner + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek ([int]$policy.weekly_audit_dow) -At $policy.weekly_audit_time
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Read-only weekly audit of the lake against public sources; writes a report, never modifies data.'
Register-ScheduledTask -TaskName $policy.weekly_audit_task_name -InputObject $task -Force | Out-Null
if ($RunNow) { Start-ScheduledTask -TaskName $policy.weekly_audit_task_name }
$installed = Get-ScheduledTask -TaskName $policy.weekly_audit_task_name
$info = Get-ScheduledTaskInfo -TaskName $policy.weekly_audit_task_name
$record = [ordered]@{ task_name=$installed.TaskName; state=[string]$installed.State; user=$installed.Principal.UserId; next_run=$info.NextRunTime.ToString('o'); last_result=$info.LastTaskResult; trigger="$($policy.weekly_audit_dow) at $($policy.weekly_audit_time) Beijing time"; timezone=(Get-TimeZone).Id; read_only=$true }
$directory = Join-Path $root 'logs\selection_update'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $directory 'audit_task_registration.json') -Encoding UTF8
$record | ConvertTo-Json -Depth 5
