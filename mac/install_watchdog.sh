#!/bin/sh
# Watchdog cho crawler Foody (an_uong_quanh_day#1388): launchd chạy common/watchdog.py mỗi 15 phút.
# Crawler chết (và không có foody_data/STOP) -> launchctl kickstart/bootstrap aqd.foody rồi báo Slack.
#   sh mac/install_watchdog.sh              # cài / cài lại
#   sh mac/install_watchdog.sh --uninstall  # gỡ
set -eu
ROOT="${AQD_FOODY_ROOT:-$HOME/script_foody}"
LABEL=aqd.watchdog.foody
PL="$HOME/Library/LaunchAgents/$LABEL.plist"
U=$(id -u)
if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true; rm -f "$PL"; echo "Đã gỡ $LABEL"; exit 0
fi
[ -f "$ROOT/common/watchdog.py" ] || { echo "Không thấy $ROOT/common/watchdog.py - chạy mac/install.sh trước" >&2; exit 1; }
CRAWLER_PL="$HOME/Library/LaunchAgents/aqd.foody.plist"
RESTART="launchctl kickstart gui/$U/aqd.foody || launchctl bootstrap gui/$U $CRAWLER_PL"
mkdir -p "$(dirname "$PL")"
cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$ROOT/.venv/bin/python</string><string>$ROOT/common/watchdog.py</string>
    <string>--crawler</string><string>foody</string>
    <string>--state-dir</string><string>$ROOT/foody_data</string>
    <string>--match</string><string>foody_fetch\.py</string>
    <string>--restart</string><string>$RESTART</string>
    <string>--stop-file</string><string>$ROOT/foody_data/STOP</string>
    <string>--log</string><string>$ROOT/foody_data/watchdog.log</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>WorkingDirectory</key><string>$ROOT</string>
</dict></plist>
PLIST
launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$U" "$PL"
echo "Đã cài $LABEL (mỗi 15 phút). Log: $ROOT/foody_data/watchdog.log | Gỡ: sh mac/install_watchdog.sh --uninstall"
