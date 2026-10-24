"""Re-fetch the menu/dish photos of Foody places that went up with NO photos (an_uong_quanh_day#1081).

Background: before #1081 a failed gallery fetch (503 / System Busy / empty-on-error) still marked the place done,
so ~870 places (Mac + Admin) were imported with zero photos although Foody lists photos for them.
The re-fetch follows the #1082 rule: ONLY menu photos first, then dish photos, AQD_FOODY_MAX_PHOTOS (5) in total.

It does NOT talk to Foody or the server itself. It hands the places to the crawler's own gallery-retry queue
(_state.json "gallery_retry", #1081), so the normal crawler re-fetches them with its usual delay/backoff and its
pusher uploads only the missing photos to the existing restaurant (no new restaurant, no duplicate photos).

    # 1. build the ID list from this machine's crawler records (read-only)
    python scripts/replay_missing_photos.py list --out scripts/replay_ids/foody_<machine>.tsv
    # 2. dry run (default): what would be queued; reads files only
    python scripts/replay_missing_photos.py enqueue --ids-file scripts/replay_ids/foody_<machine>.tsv
    # 3. for real, ONLY with the crawler stopped (STOP file, process gone), then start the crawler again
    python scripts/replay_missing_photos.py enqueue --ids-file ... --apply [--limit N]

--apply refuses to run while foody_fetch.py is running or _state.json changed in the last 120 s (the crawler keeps
_state.json in memory and would overwrite the edit). It writes a backup _state.json.pre-replay-<time> first.
The crawler then retries AQD_FOODY_GALLERY_RETRY_PER_PAGE (default 3) queued places before each list page.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_ROOT = os.environ.get("AQD_FOODY_DATA", os.path.join(REPO, "foody_data"))
MENU, FOOD = 3, 2
BASE = "https://www.foody.vn"


def _read(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def album_counts(place):
    out = {}
    for k, v in (place.get("albums") or {}).items():
        try:
            out[int(k)] = int(v or 0)
        except (TypeError, ValueError):
            pass
    return out


def classify(place, rec):
    """-> (candidate?, reason, menu_count, food_count)."""
    if not rec or not rec.get("restaurant_id"):
        return False, "not_imported", 0, 0
    if rec.get("photos"):
        return False, "has_photos", 0, 0
    a = album_counts(place)
    menu, food = a.get(MENU, 0), a.get(FOOD, 0)
    if a:
        if menu + food > 0:
            return True, "menu_or_dish_at_source", menu, food
        return False, "only_venue_or_other_albums", menu, food
    if int(place.get("total_pictures") or 0) > 0:
        return True, "photos_at_source_albums_unknown", 0, 0
    return False, "no_photos_at_source", 0, 0


def build_list(root):
    up = _read(os.path.join(root, "_uploaded.json"), {})
    rows, why = [], {}
    for fid in sorted(os.listdir(root), key=lambda x: (len(x), x)):
        pj = os.path.join(root, fid, "place.json")
        if not fid.isdigit() or not os.path.isfile(pj):
            continue
        place = _read(pj, None)
        if not place:
            why["bad_place_json"] = why.get("bad_place_json", 0) + 1; continue
        ok, reason, menu, food = classify(place, up.get(fid))
        why[reason] = why.get(reason, 0) + 1
        if ok:
            rows.append((fid, str(up[fid]["restaurant_id"]), menu, food, int(place.get("total_pictures") or 0)))
    return rows, why


def write_list(path, rows, why, root):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# foody places imported with 0 photos but menu/dish photos at the source; {len(rows)} ids\n")
        f.write(f"# built {time.strftime('%Y-%m-%d %H:%M %z')} from {root}; counts: {json.dumps(why, sort_keys=True)}\n")
        f.write("# foody_id\trestaurant_id\tmenu_photos_at_source\tdish_photos_at_source\ttotal_pictures\n")
        for r in rows:
            f.write("\t".join(str(x) for x in r) + "\n")


def read_ids(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line.split("\t")[0])
    return out


_CRAWLER_SCRIPT = re.compile(r"""(?:^|[\\/\s"'])foody_fetch\.py(?:["'\s]|$)""", re.I)
_FIRST_TOKEN = re.compile(r"""^\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")


def _exe_name(cmd):
    """Basename of the executable of a command line (quoted or not), lowercased."""
    m = _FIRST_TOKEN.match(cmd or "")
    exe = next((x for x in (m.groups() if m else ()) if x), "")
    return re.split(r"[\\/]", exe)[-1].lower()


def crawler_pids(procs, exclude=()):
    """an_uong_quanh_day#1106: PIDs of real crawler processes in `procs` [(pid, ppid, command line)].

    A crawler is a python interpreter whose command line runs foody_fetch.py. Not a crawler: this script and its
    parent (`exclude`), the process-list helper itself (a PowerShell/ps whose own command line mentions the name,
    the Windows bug that always answered "running"), shells/editors that only mention the file, and replay runs."""
    skip = {int(x) for x in exclude if x is not None}
    out = []
    for pid, _ppid, cmd in procs:
        if pid is None or int(pid) in skip or not cmd:
            continue
        if not _exe_name(cmd).startswith("python"):
            continue
        if "replay_missing_photos" in cmd or not _CRAWLER_SCRIPT.search(cmd):
            continue
        out.append(int(pid))
    return out


def _list_processes(nt=None):
    """[(pid, ppid, command line)] of all processes. The listing command never names the crawler."""
    if (os.name == "nt") if nt is None else nt:
        ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | "
              "ConvertTo-Json -Compress")
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        rows = json.loads(raw or "[]")
        rows = [rows] if isinstance(rows, dict) else rows
        return [(r.get("ProcessId"), r.get("ParentProcessId"), r.get("CommandLine") or "") for r in rows]
    raw = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=30).stdout
    out = []
    for line in raw.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            out.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
    return out


def crawler_running():
    """True / False / None (unknown). Looks for a real foody_fetch.py crawler, never this script or its helper."""
    try:
        return bool(crawler_pids(_list_processes(), exclude=(os.getpid(), os.getppid())))
    except Exception:
        return None


def item_for(place, fid):
    url = (place.get("source_url") or "").replace(BASE, "")
    return {"Id": int(fid), "Url": url, "Phone": place.get("list_phone"), "Address": place.get("address_full"),
            "AvgRating": place.get("rating"), "TotalReviews": place.get("review_count"), "LstReview": []}


def plan(root, ids, st):
    up = _read(os.path.join(root, "_uploaded.json"), {})
    todo, skip = [], {}
    for fid in ids:
        place = _read(os.path.join(root, fid, "place.json"), None)
        if not place:
            skip["no_place_json_here"] = skip.get("no_place_json_here", 0) + 1; continue
        ok, reason, menu, food = classify(place, up.get(fid))
        if not ok:
            skip[reason] = skip.get(reason, 0) + 1; continue
        if fid in (st.get("gallery_retry") or {}):
            skip["already_queued"] = skip.get("already_queued", 0) + 1; continue
        if not item_for(place, fid)["Url"].startswith("/"):
            skip["no_source_url"] = skip.get("no_source_url", 0) + 1; continue
        place["_restaurant_id"] = up[fid]["restaurant_id"]
        todo.append((fid, place, menu, food))
    return todo, skip


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("list", help="build the ID list from crawler records (read-only)")
    a1.add_argument("--root", default=DEFAULT_ROOT); a1.add_argument("--out", required=True)
    a2 = sub.add_parser("enqueue", help="queue the IDs for the crawler's gallery retry (dry run unless --apply)")
    a2.add_argument("--root", default=DEFAULT_ROOT); a2.add_argument("--ids-file", required=True)
    a2.add_argument("--apply", action="store_true"); a2.add_argument("--limit", type=int, default=0)
    a2.add_argument("--max-photos", type=int, default=int(os.environ.get("AQD_FOODY_MAX_PHOTOS", "5")))
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print(f"no data dir {root}"); return 2
    if a.cmd == "list":
        rows, why = build_list(root)
        write_list(a.out, rows, why, root)
        print(f"{len(rows)} ids -> {a.out}; {json.dumps(why, sort_keys=True)}")
        return 0
    stp = os.path.join(root, "_state.json")
    st = _read(stp, {})
    ids = read_ids(a.ids_file)
    todo, skip = plan(root, ids, st)
    if a.limit:
        todo = todo[:a.limit]
    exp = sum(min(a.max_photos, m + f) for _, _, m, f in todo)
    print(f"{len(ids)} ids in {a.ids_file}: {len(todo)} to queue, skipped {json.dumps(skip, sort_keys=True)}")
    print(f"rule: menu photos first, then dish photos, max {a.max_photos} per place -> up to {exp} photos")
    for fid, place, m, f in todo[:10]:
        print(f"  #{fid} -> restaurant {place['_restaurant_id']} | menu {m} dish {f} at source | {place.get('name')}")
    if len(todo) > 10:
        print(f"  ... {len(todo) - 10} more")
    if not a.apply:
        print("DRY RUN: nothing written. Re-run with --apply only while the crawler is stopped.")
        return 0
    run = crawler_running()
    if run or run is None:
        print("REFUSED: foody_fetch.py is running (or the check failed). Stop the crawler first (STOP file)."); return 3
    if os.path.exists(stp) and time.time() - os.path.getmtime(stp) < 120:
        print("REFUSED: _state.json changed in the last 120 s; the crawler may still be running."); return 3
    if not todo:
        print("nothing to queue"); return 0
    bak = f"{stp}.pre-replay-{time.strftime('%Y%m%d-%H%M%S')}"
    if os.path.exists(stp):
        shutil.copy2(stp, bak); print(f"backup -> {bak}")
    st.setdefault("done", {}); st.setdefault("failed", {})
    q = st.setdefault("gallery_retry", {})
    for fid, place, _, _ in todo:
        q[fid] = {"tries": 0, "item": item_for(place, fid), "error": "replay: imported with 0 photos (#1081)",
                  "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "next_at": 0}
    tmp = stp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, stp)
    print(f"queued {len(todo)} places in {stp} gallery_retry; start the crawler to process them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
