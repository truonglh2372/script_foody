#!/usr/bin/env bash
# Cài + chạy script_foody bằng 1 lệnh (macOS):
#   curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_foody/main/mac/install.sh | bash
# Biến tuỳ chọn: AQD_FOODY_ROOT (mặc định ~/script_foody), AQD_FOODY_LIMIT (mặc định 20, 0 = chạy hết),
#   AQD_FOODY_PARITY (mặc định odd; even/all), AQD_FOODY_CLIP=1 (cài CLIP lọc ảnh), AQD_FOODY_NORUN=1 (chỉ cài, không chạy)
set -euo pipefail
ROOT="${AQD_FOODY_ROOT:-$HOME/script_foody}"
REPO="https://github.com/truonglh2372/script_foody.git"
command -v git >/dev/null || { echo "Cần git (xcode-select --install)"; exit 1; }
PY="$(command -v python3 || true)"; [ -n "$PY" ] || { echo "Cần python3"; exit 1; }
if [ -d "$ROOT/.git" ]; then
  touch "$ROOT/foody_data/STOP" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/aqd.foody" 2>/dev/null || true
  pkill -f "$ROOT/common/foody_fetch.py" 2>/dev/null || true
  git -C "$ROOT" pull --ff-only
else
  git clone --depth 1 "$REPO" "$ROOT"
fi
cd "$ROOT"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
[ "${AQD_FOODY_CLIP:-0}" = "1" ] && .venv/bin/pip install -q -r requirements-clip.txt
mkdir -p foody_data; rm -f foody_data/STOP
[ "${AQD_FOODY_NORUN:-0}" = "1" ] && { echo "Đã cài ở $ROOT"; exit 0; }
CLIPFLAG=""; [ "${AQD_FOODY_CLIP:-0}" = "1" ] || CLIPFLAG="--no-clip"
PARITY="${AQD_FOODY_PARITY:-odd}"; LIMIT="${AQD_FOODY_LIMIT:-20}"
if [ "$LIMIT" = "0" ]; then
  # chạy hết: launchd, tự bật lại khi khởi động
  PL="$HOME/Library/LaunchAgents/aqd.foody.plist"; mkdir -p "$(dirname "$PL")"
  EXTRA=""; [ -n "$CLIPFLAG" ] && EXTRA="<string>$CLIPFLAG</string>"
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>aqd.foody</string>
  <key>ProgramArguments</key><array>
    <string>$ROOT/.venv/bin/python</string><string>-u</string><string>$ROOT/common/foody_fetch.py</string>
    <string>--limit</string><string>0</string><string>--parity</string><string>$PARITY</string>$EXTRA
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$ROOT/foody_data/fetch.log</string>
  <key>StandardErrorPath</key><string>$ROOT/foody_data/fetch.log</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONIOENCODING</key><string>utf-8</string><key>PYTHONUTF8</key><string>1</string></dict>
</dict></plist>
PLIST
  launchctl bootstrap "gui/$(id -u)" "$PL"
  echo "Đang chạy nền (launchd aqd.foody, parity $PARITY). Log: $ROOT/foody_data/fetch.log | Dừng: launchctl bootout gui/\$(id -u)/aqd.foody"
else
  nohup .venv/bin/python -u common/foody_fetch.py --limit "$LIMIT" --parity "$PARITY" $CLIPFLAG >> foody_data/fetch.log 2>&1 &
  echo "Đang chạy (pid $!, parity $PARITY). Log: $ROOT/foody_data/fetch.log  | Dừng: touch $ROOT/foody_data/STOP"
fi
