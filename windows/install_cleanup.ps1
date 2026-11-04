# Tu don o cho crawler foody (an_uong_quanh_day#1388, sau disk_low 04/11): Task Scheduler chay disk_cleanup.py moi 15 phut;
# script tu quyet: don dinh ky moi AQD_CLEANUP_EVERY_H gio (6) hoac ngay khi o < AQD_CLEANUP_MIN_FREE_GB (8).
# Chi xoa thu tao lai duoc (xem dau disk_cleanup.py); moi lan don gui 1 dong Slack. Log: $Root\foody_data\cleanup.log
#   powershell -ExecutionPolicy Bypass -File windows\install_cleanup.ps1            # cai / cai lai
#   powershell -ExecutionPolicy Bypass -File windows\install_cleanup.ps1 -Uninstall # go
#   powershell -ExecutionPolicy Bypass -File windows\install_cleanup.ps1 -DryRun    # chi liet ke, khong cai, khong xoa
param([switch]$Uninstall, [switch]$DryRun, [int]$Minutes = 15)
$ErrorActionPreference = 'Stop'
$Root = if ($env:AQD_FOODY_ROOT) { $env:AQD_FOODY_ROOT } else { 'C:\script_foody' }
$Task = 'aqd_cleanup_foody'
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
  "Da go $Task"; return
}
$Mod = "$Root\common\disk_cleanup.py"
if (-not (Test-Path $Mod)) { throw "Khong thay $Mod - cai crawler truoc" }
$Py = "$Root\.venv\Scripts\pythonw.exe"
if (-not (Test-Path $Py)) { $Py = "$Root\.venv\Scripts\python.exe" }
if ($DryRun) {
  $PyCon = $Py -replace 'pythonw\.exe$', 'python.exe'
  & $PyCon $Mod --crawler foody --root $Root --dry-run; return
}
$Arg = "`"$Mod`" --crawler foody --root `"$Root`" --log `"$Root\foody_data\cleanup.log`""
$Action = New-ScheduledTaskAction -Execute $Py -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -Priority 7
Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
"Da cai $Task (kiem tra moi $Minutes phut). Log: $Root\foody_data\cleanup.log | Go: -Uninstall"
