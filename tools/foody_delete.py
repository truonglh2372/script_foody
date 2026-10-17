"""Xoá TOÀN BỘ dữ liệu nguồn Foody trên server (issue an_uong_quanh_day#682) và/hoặc trên máy.

  python tools/foody_delete.py               # dry-run: server trả số quán/ảnh/món sẽ bị xoá
  python tools/foody_delete.py --yes         # xoá thật trên server (gửi confirm = số quán trong dry-run)
  python tools/foody_delete.py --local       # xoá thư mục foody_data/ trên máy này
"""
import argparse, os, shutil, sys
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(HERE, "..", "common"))
ROOT = os.environ.get("AQD_FOODY_DATA", os.path.join(HERE, "..", "foody_data"))
EP = "/api/v1/admin/imports/foody"

ap = argparse.ArgumentParser(); ap.add_argument("--yes", action="store_true"); ap.add_argument("--local", action="store_true")
a = ap.parse_args()
if a.local:
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT); print("đã xoá", ROOT)
    else:
        print("không có", ROOT)
    sys.exit(0)
from aqd_api import AQD
api = AQD()
r = api.call("DELETE", EP + "?dry_run=true", raw_status=(404,))
if r.get("_status") == 404:
    sys.exit("Server chưa có endpoint xoá theo nguồn (chờ issue #682).")
print("Sẽ xoá:", {k: v for k, v in r.items() if not k.startswith("_")})
if not a.yes:
    sys.exit("DRY-RUN. Chạy lại với --yes để xoá thật.")
n = r.get("restaurants", 0)
print(api.call("DELETE", f"{EP}?dry_run=false&confirm={n}"))
