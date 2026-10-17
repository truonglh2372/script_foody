# Cài + chạy script_foody bằng 1 lệnh (Windows PowerShell):
#   irm https://raw.githubusercontent.com/truonglh2372/script_foody/main/windows/install.ps1 | iex
# Biến tuỳ chọn: $env:AQD_FOODY_ROOT (mặc định C:\script_foody), $env:AQD_FOODY_LIMIT (20; 0 = chạy hết),
#   $env:AQD_FOODY_CLIP=1 (cài CLIP lọc ảnh), $env:AQD_FOODY_NORUN=1 (chỉ cài)
$ErrorActionPreference = 'Stop'
$Root = if ($env:AQD_FOODY_ROOT) { $env:AQD_FOODY_ROOT } else { 'C:\script_foody' }
$Limit = if ($env:AQD_FOODY_LIMIT) { $env:AQD_FOODY_LIMIT } else { '20' }
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
if ($env:AQD_FOODY_CLIP -eq '1') { & $V -m pip install -q -r requirements-clip.txt }
Remove-Item "$Root\foody_data\STOP" -Force -ErrorAction SilentlyContinue
if ($env:AQD_FOODY_NORUN -eq '1') { "Đã cài ở $Root"; return }
$a = @('-u', "$Root\common\foody_fetch.py", '--limit', $Limit)
if ($env:AQD_FOODY_CLIP -ne '1') { $a += '--no-clip' }
$p = Start-Process -FilePath $V -ArgumentList $a -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$Root\foody_data\fetch.log" -RedirectStandardError "$Root\foody_data\fetch.err.log"
"Đang chạy (pid $($p.Id)). Log: $Root\foody_data\fetch.log | Dừng: New-Item $Root\foody_data\STOP"
