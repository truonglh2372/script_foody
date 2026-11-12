# Is foody already running for THIS root? Called by Startup\aqd_foody.cmd (logon + foody watchdog restart) before it
# starts foody_fetch.py. Exit 0 = running (the .cmd does nothing), exit 1 = not running or cannot tell (the .cmd starts it).
# Same idea as script_google_map windows\start_fetcher.ps1: a CIM query on REAL python processes only
# (Name like 'python%'), never matching this check itself, its cmd/powershell parent, or the "cmd /c ... foody_fetch.py"
# wrapper (the old check matched its own command line, so after the 12/11 reboot it always said "already running").
# The match rule is mirrored in common/foody_running.py (unit tests: tests/test_foody_running.py) - keep both in sync.
# ASCII only on purpose (Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI).
param([string]$Root = 'C:\script_foody')
function N($p){ if(-not $p){ return '' }; ([string]$p).Replace('\','/').Replace('"','').ToLowerInvariant() }
$rootN = (N $Root).TrimEnd('/') + '/'
function IsFoody([string]$name, [string]$cmd){
  if(-not ((N $name) -like 'python*')){ return $false }
  $c = N $cmd
  if($c -notmatch 'foody_fetch\.py'){ return $false }
  # an absolute path to foody_fetch.py must be under this root; a relative one (started by hand in the root) counts
  $abs = [regex]::Matches($c, '[a-z]:/[^ ]*foody_fetch\.py')
  if($abs.Count -eq 0){ return $true }
  foreach($m in $abs){ if($m.Value.StartsWith($rootN)){ return $true } }
  return $false
}
try {
  $skip = @([int]$PID)
  $me = Get-CimInstance Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop
  if($me){ $skip += [int]$me.ParentProcessId }
  $hits = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction Stop | Where-Object {
    ($skip -notcontains [int]$_.ProcessId) -and (IsFoody $_.Name $_.CommandLine) })
} catch {
  Write-Output ("{0} foody_running: cannot list processes ({1}) - assuming not running" -f (Get-Date -f 'yyyy-MM-dd HH:mm:ss'), $_.Exception.Message)
  exit 1
}
if($hits.Count -gt 0){
  Write-Output ("{0} foody already running for {1}: PID {2} - not starting another" -f (Get-Date -f 'yyyy-MM-dd HH:mm:ss'), $Root, (($hits | ForEach-Object { $_.ProcessId }) -join ','))
  exit 0
}
Write-Output ("{0} foody not running for {1} - starting it" -f (Get-Date -f 'yyyy-MM-dd HH:mm:ss'), $Root)
exit 1
