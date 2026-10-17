#!/usr/bin/env bash
# Cài + chạy script_foody bằng 1 lệnh (macOS):
#   curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_foody/main/mac/install.sh | bash
# Biến tuỳ chọn: AQD_FOODY_ROOT (mặc định ~/script_foody), AQD_FOODY_LIMIT (mặc định 20, 0 = chạy hết),
#   AQD_FOODY_CLIP=1 (cài CLIP lọc ảnh), AQD_FOODY_NORUN=1 (chỉ cài, không chạy)
set -euo pipefail
ROOT="${AQD_FOODY_ROOT:-$HOME/script_foody}"
REPO="https://github.com/truonglh2372/script_foody.git"
command -v git >/dev/null || { echo "Cần git (xcode-select --install)"; exit 1; }
PY="$(command -v python3 || true)"; [ -n "$PY" ] || { echo "Cần python3"; exit 1; }
if [ -d "$ROOT/.git" ]; then
  touch "$ROOT/foody_data/STOP" 2>/dev/null || true
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
nohup .venv/bin/python -u common/foody_fetch.py --limit "${AQD_FOODY_LIMIT:-20}" $CLIPFLAG >> foody_data/fetch.log 2>&1 &
echo "Đang chạy (pid $!). Log: $ROOT/foody_data/fetch.log  | Dừng: touch $ROOT/foody_data/STOP"
