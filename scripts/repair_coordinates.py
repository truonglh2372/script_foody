"""Repair the points of Foody restaurants that went up with broken coordinates (script_foody#18, an_uong_quanh_day#1452).

Why not a plain re-crawl: a re-import (fill mode) never overwrites an existing point, and Foody's OWN page data is wrong
for some of these places (checked 2026-11-08: Latitude 0.7556710 for 36 Huynh Tan Phat; Latitude = Longtitude =
10.7433766 for 6 Chung Cu Pham The Hien). So per row this script re-reads the Foody page and decides:

  patch   Foody now gives a point inside Vietnam and the server point is broken -> PATCH latitude/longitude (--apply)
  manual  Foody still gives a broken point -> nothing is written; the row is listed with its address and Foody URL so a
          person sets the pin in the admin (or a geocoder is used). A point is never guessed from the bad one.
  ok      the server point is already valid -> never overwritten

Dry run by default (reads Foody, never calls the server). --apply needs creds.env like the crawler.

    python scripts/repair_coordinates.py --ids-file scripts/replay_ids/foody_bad_coords_1452.tsv            # dry run
    python scripts/repair_coordinates.py --ids-file scripts/replay_ids/foody_bad_coords_1452.tsv --apply

TSV columns (tab separated, '#' lines ignored): restaurant_id, foody_source_id, foody_url.
"""
from __future__ import annotations
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))


def read_rows(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rid, sid, url = (line.split("\t") + ["", "", ""])[:3]
        rows.append({"restaurant_id": int(rid), "source_id": sid.strip(), "url": url.strip()})
    return rows


def plan(foody_lat, foody_lng, server_lat, server_lng):
    """-> "ok" | "patch" | "manual". Pure: no network."""
    import foody_upload as fu
    if fu.coords_problem(server_lat, server_lng) is None:
        return "ok"
    return "patch" if fu.coords_problem(foody_lat, foody_lng) is None else "manual"


def run(rows, fetch_detail, get_server_point, patch, apply=False, out=print):
    """fetch_detail(row) -> parse_detail dict | None; get_server_point(rid) -> (lat, lng); patch(rid, lat, lng)."""
    import foody_upload as fu
    stats = {"ok": 0, "patch": 0, "patched": 0, "manual": 0, "fail": 0}
    for row in rows:
        rid = row["restaurant_id"]
        try:
            det = fetch_detail(row)
            if det is None:
                stats["fail"] += 1; out(f"[fail]   {rid} foody #{row['source_id']}: page not found {row['url']}"); continue
            f_lat, f_lng = det.get("lat"), det.get("lng")
            s_lat, s_lng = get_server_point(rid)
            action = plan(f_lat, f_lng, s_lat, s_lng)
            stats[action] += 1
            if action == "ok":
                out(f"[ok]     {rid}: server point ({s_lat}, {s_lng}) is valid, untouched")
            elif action == "manual":
                out(f"[manual] {rid} foody #{row['source_id']}: Foody still says ({f_lat}, {f_lng}) = "
                    f"{fu.coords_problem(f_lat, f_lng)}; address {det.get('address')!r}, {row['url']}")
            elif not apply:
                out(f"[patch]  {rid}: ({s_lat}, {s_lng}) -> ({f_lat}, {f_lng}) (dry run)")
            else:
                patch(rid, float(f_lat), float(f_lng)); stats["patched"] += 1
                out(f"[patched] {rid}: ({s_lat}, {s_lng}) -> ({f_lat}, {f_lng})")
        except Exception as e:  # one row never stops the others
            stats["fail"] += 1; out(f"[fail]   {rid}: {type(e).__name__} {str(e)[:200]}")
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ids-file", required=True)
    ap.add_argument("--apply", action="store_true", help="PATCH the points Foody now gives right (default: dry run)")
    a = ap.parse_args(argv)
    import foody_fetch as ff
    c = ff.Client()
    api = None
    if a.apply:
        from foody_push import load_creds
        load_creds()
        from aqd_api import AQD
        api = AQD()

    def fetch_detail(row):
        path = row["url"].replace(ff.BASE, "") if row["url"] else None
        r = c.get(path) if path else None
        return ff.parse_detail(r.text) if r is not None else None

    def server_point(rid):
        if api is None:  # dry run does not call the server: treat the stored point as unknown-broken
            return None, None
        r = api.restaurant(rid)
        return r.get("latitude"), r.get("longitude")

    def patch(rid, lat, lng):
        res = api.update_info(rid, {"latitude": lat, "longitude": lng})
        if not res.get("ok"):
            raise RuntimeError(f"server did not keep the point: {res.get('mismatched')}")

    st = run(read_rows(a.ids_file), fetch_detail, server_point, patch, apply=a.apply)
    print("TOTAL", st)
    if not a.apply:
        print("DRY RUN: nothing was sent to the server. Add --apply to PATCH the [patch] rows.")


if __name__ == "__main__":
    main()
