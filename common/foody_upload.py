"""Đẩy dữ liệu Foody (foody_data/) lên admin API — MẶC ĐỊNH DRY-RUN.

Chỉ dùng endpoint import mới (issue an_uong_quanh_day#683, cột nguồn ở #682).
Khi endpoint chưa có (404) script dừng, KHÔNG ghi qua endpoint cũ.
Mọi quán/ảnh/món gửi lên đều mang source='foody', source_id, source_url để xoá hàng loạt sau.

  python common/foody_upload.py                 # dry-run: in payload tóm tắt, ghi foody_data/_upload_preview.jsonl
  python common/foody_upload.py --apply         # gửi thật (cần endpoint #683)
  python common/foody_upload.py --apply --ids 30086 44253
"""
import argparse, json, os, sys, uuid
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.environ.get("AQD_FOODY_DATA", os.path.join(HERE, "..", "foody_data"))
EP = os.environ.get("AQD_FOODY_IMPORT_EP", "/api/v1/admin/imports/restaurants")
DRINK_HINT = ("trà", "cà phê", "cafe", "coffee", "sinh tố", "nước", "soda", "bia", "sữa", "juice", "tea", "đá xay")


def payload(p):
    return {
        "source": "foody", "source_id": p["source_id"], "source_url": p["source_url"],
        "name": p.get("name"), "address": p.get("address_full") or p.get("address"),
        "district": p.get("district"), "area": p.get("area"), "lat": p.get("lat"), "lng": p.get("lng"),
        "phones": p.get("phones") or [], "website": p.get("website"),
        "price_min": p.get("price_min"), "price_max": p.get("price_max"),
        "cuisines": p.get("cuisines") or [], "categories": p.get("categories") or [],
        "opening_periods": p.get("opening_periods") or [],
        "rating": p.get("rating"), "review_count": p.get("review_count"),
        "menu": [{"category": m.get("category"), "name": m.get("name"), "price": m.get("price"),
                  "kind": "drink" if any(h in ((m.get("category") or "") + " " + (m.get("name") or "")).lower() for h in DRINK_HINT) else "food",
                  "source": "foody"} for m in p.get("menu") or [] if m.get("name")],
    }


def reviews_of(p):
    """Review Foody: luôn mang source='foody' + source_id. CHƯA gửi lên API (chờ #682),
    chỉ ghi vào foody_data/_reviews_pending.jsonl ở trạng thái pending_api."""
    out = []
    for x in p.get("reviews_sample") or []:
        if not x.get("source_id"):
            continue  # không có id nguồn thì không bao giờ gửi (không xoá được)
        out.append({"source": "foody", "source_id": x["source_id"], "source_url": x.get("source_url"),
                    "author_name": x.get("author"), "rating": x.get("rating"), "text": x.get("text"),
                    "restaurant_source_id": p["source_id"], "status": "pending_api"})
    return out


def photos_of(p):
    return [x for x in p.get("photos") or [] if x.get("keep") and x.get("file")]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); ap.add_argument("--ids", nargs="*")
    a = ap.parse_args()
    ids = a.ids or sorted(d for d in os.listdir(ROOT) if os.path.exists(os.path.join(ROOT, d, "place.json")))
    log = os.path.join(ROOT, "_uploaded.json")
    done = json.load(open(log, encoding="utf-8")) if os.path.exists(log) else {}
    api = None
    if a.apply:
        from aqd_api import AQD
        api = AQD()
        probe = api.call("GET", EP + "?source=foody&limit=1", raw_status=(404, 405))
        if isinstance(probe, dict) and probe.get("_status") == 404:
            sys.exit(f"Endpoint {EP} chưa có trên server (chờ issue #683). Không gửi gì.")
    prev = open(os.path.join(ROOT, "_upload_preview.jsonl"), "w", encoding="utf-8")
    revf = open(os.path.join(ROOT, "_reviews_pending.jsonl"), "w", encoding="utf-8")
    n = 0
    for fid in ids:
        p = json.load(open(os.path.join(ROOT, fid, "place.json"), encoding="utf-8"))
        body, ph = payload(p), photos_of(p)
        rv = reviews_of(p)
        for r_ in rv:
            revf.write(json.dumps(r_, ensure_ascii=False) + "\n")
        prev.write(json.dumps({**body, "photos": [x["source_id"] for x in ph],
                               "reviews_pending": [r_["source_id"] for r_ in rv]}, ensure_ascii=False) + "\n")
        if not a.apply:
            print(f"[dry] #{fid} {body['name']} | {len(body['opening_periods'])} ca | {len(body['menu'])} món | {len(ph)} ảnh")
            continue
        if fid in done:
            continue
        r = api.call("POST", EP, json_body=body)
        rid = r["restaurant_id"]
        dup = (r.get("duplicate") or {}).get("candidates") or []
        for x in ph:
            path = os.path.join(ROOT, fid, x["file"])
            with open(path, "rb") as f:
                api.call("POST", f"{EP}/{rid}/photos",
                         data={"source": "foody", "source_id": x["source_id"], "source_url": x["source_url"],
                               "role": x.get("role") or x.get("album")},
                         files={"file": (os.path.basename(path), f, "image/jpeg")})
        done[fid] = {"restaurant_id": rid, "duplicate_of": [c.get("restaurant_id") for c in dup]}
        json.dump(done, open(log, "w", encoding="utf-8"), indent=1)
        n += 1
        print(f"✓ #{fid} -> quán {rid}" + (f" (NGHI TRÙNG với {done[fid]['duplicate_of']})" if dup else ""))
    print(("Đã gửi %d quán" % n) if a.apply else "DRY-RUN: không gửi gì. Thêm --apply để gửi thật.")


if __name__ == "__main__":
    main()
