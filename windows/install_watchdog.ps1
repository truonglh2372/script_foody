# Watchdog cho crawler Foody (an_uong_quanh_day#1388): Task Scheduler chạy common\watchdog.py mỗi 15 phút.
# Crawler chết (và không có foody_data\STOP) -> chạy lại Startup\aqd_foody.cmd rồi báo Slack.
#   powershell -ExecutionPolicy Bypass -File windows\install_watchdog.ps1            # cài / cài lại
#   powershell -ExecutionPolicy Bypass -File windows\install_watchdog.ps1 -Uninstall # gỡ
param([switch]$Uninstall, [int]$Minutes = 15)
$ErrorActionPreference = 'Stop'
$Root = if ($env:AQD_FOODY_ROOT) { $env:AQD_FOODY_ROOT } else { 'C:\script_foody' }
$Task = 'aqd_watchdog_foody'
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
  "Da go $Task"; return
}
$Py = "$Root\.venv\Scripts\pythonw.exe"
if (-not (Test-Path $Py)) { $Py = "$Root\.venv\Scripts\python.exe" }
if (-not (Test-Path "$Root\common\watchdog.py")) { throw "Khong thay $Root\common\watchdog.py - chay windows\install.ps1 truoc" }
$Cmd = Join-Path ([Environment]::GetFolderPath('Startup')) 'aqd_foody.cmd'
if (-not (Test-Path $Cmd)) { throw "Khong thay $Cmd - chay windows\install.ps1 (che do chay het) truoc" }
$Data = "$Root\foody_data"
$Arg = "`"$Root\common\watchdog.py`" --crawler foody --state-dir `"$Data`" --match `"foody_fetch\.py`" " +
       "--restart `"\`"$Cmd\`"`" --stop-file `"$Data\STOP`" --log `"$Data\watchdog.log`""
$Action = New-ScheduledTaskAction -Execute $Py -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
"Da cai $Task (moi $Minutes phut). Log: $Data\watchdog.log | Go: -Uninstall"
