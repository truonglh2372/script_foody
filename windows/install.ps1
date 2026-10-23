# Cài + chạy script_foody bằng 1 lệnh (Windows PowerShell):
#   irm https://raw.githubusercontent.com/truonglh2372/script_foody/main/windows/install.ps1 | iex
# Biến tuỳ chọn: $env:AQD_FOODY_ROOT (mặc định C:\script_foody), $env:AQD_FOODY_LIMIT (20; 0 = chạy hết),
#   $env:AQD_FOODY_PARITY (mac dinh even; odd/all), $env:AQD_FOODY_NORUN=1 (chỉ cài)
$ErrorActionPreference = 'Stop'
$Root = if ($env:AQD_FOODY_ROOT) { $env:AQD_FOODY_ROOT } else { 'C:\script_foody' }
$Limit = if ($env:AQD_FOODY_LIMIT) { $env:AQD_FOODY_LIMIT } else { '20' }
$Parity = if ($env:AQD_FOODY_PARITY) { $env:AQD_FOODY_PARITY } else { 'even' }
$Zip = 'https://github.com/truonglh2372/script_foody/archive/refs/heads/main.zip'
$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) { $PyExe = 'py'; $PyArgs = @('-3') } else {
  $p = Get-Command python -ErrorAction SilentlyContinue
  if (-not $p) { winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements; $env:Path += ";$env:LOCALAPPDATA\Programs\Python\Python312" }
  $PyExe = 'python'; $PyArgs = @() }
New-Item -ItemType Directory -Force -Path "$Root\foody_data" | Out-Null
New-Item -ItemType File -Force -Path "$Root\foody_data\STOP" | Out-Null
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*foody_fetch.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$tmp = Join-Path $env:TEMP 'script_foody.zip'
Invoke-WebRequest $Zip -OutFile $tmp -UseBasicParsing
$ex = Join-Path $env:TEMP 'script_foody_x'; Remove-Item $ex -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive $tmp $ex -Force
Copy-Item "$ex\script_foody-main\*" $Root -Recurse -Force   # giữ nguyên foody_data\ (tiến độ)
Set-Location $Root
if (-not (Test-Path .venv)) { & $PyExe @PyArgs -m venv .venv }
$V = "$Root\.venv\Scripts\python.exe"
& $V -m pip install -q --upgrade pip
& $V -m pip install -q -r requirements.txt
Remove-Item "$Root\foody_data\STOP" -Force -ErrorAction SilentlyContinue
if ($env:AQD_FOODY_NORUN -eq '1') { "Da cai o $Root"; return }
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
# #855: khong con model local - API phan loai moi anh
if ($Limit -eq '0') {
  # chay het: autostart qua Startup folder (.cmd), giong script Google
  $Startup = [Environment]::GetFolderPath('Startup')
  $Cmd = Join-Path $Startup 'aqd_foody.cmd'
  @"
@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "$Root"
start "" /min cmd /c ""$V" -u "$Root\common\foody_fetch.py" --limit 0 --parity $Parity >> "$Root\foody_data\fetch.log" 2>&1"
"@ | Set-Content -Path $Cmd -Encoding ASCII
  Remove-Item (Join-Path $Startup 'aqd_foody.cmd.disabled') -Force -ErrorAction SilentlyContinue
  Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$Cmd`"" -WindowStyle Hidden
  "Dang chay nen (parity $Parity, autostart: $Cmd). Log: $Root\foody_data\fetch.log | Dung: New-Item $Root\foody_data\STOP"
} else {
  $a = @("-u", "$Root\common\foody_fetch.py", '--limit', $Limit, '--parity', $Parity)
  $p = Start-Process -FilePath $V -ArgumentList $a -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$Root\foody_data\fetch.log" -RedirectStandardError "$Root\foody_data\fetch.err.log"
  "Dang chay (pid $($p.Id), parity $Parity). Log: $Root\foody_data\fetch.log | Dung: New-Item $Root\foody_data\STOP"
}
