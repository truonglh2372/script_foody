"""Đẩy dữ liệu Foody (foody_data/) lên admin API — MẶC ĐỊNH DRY-RUN (chỉ xem trước, không gọi server).

Endpoint thật (an_uong_quanh_day: app/controllers/api/v1/admin/import_restaurants_controller.rb, #682/#683):
  POST /v1/admin/imports/restaurants             JSON {source:'foody', source_id, source_url, name, address, district,
                                                 city, lat, lng, website, phones[], cuisines[], categories[],
                                                 opening_periods[{weekday 1=T2..7=CN, opens_at, closes_at}],
                                                 menu[{category, name, price, kind}]}
      -> {restaurant_id, created, duplicate:{suspected, candidates:[{restaurant_id, score, reasons}]}}
      Server tự chống trùng theo (source, source_id): gửi lại = cập nhật quán cũ (created=false), không tạo bản mới.
      Nghi trùng quán khác: server tạo quán draft, gắn cờ 'duplicate' + duplicate candidate để admin duyệt.
  POST /v1/admin/imports/restaurants/:id/photos  multipart image + source, source_id, source_url, role
                                                 (food|drink|menu|interior|storefront_or_sign); gửi lại = trả ảnh cũ.
Review Foody: server CHƯA có endpoint nhận review Foody (#682/#683 không có), nên review chỉ ghi vào
foody_data/_reviews_pending.jsonl (source='foody', source_id), không gửi.

  python common/foody_upload.py                     # dry-run: kiểm tra payload, ghi foody_data/_upload_preview.jsonl
  python common/foody_upload.py --apply             # gửi thật
  python common/foody_upload.py --apply --ids 30086 44253
"""
import argparse, json, os, sys
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.environ.get("AQD_FOODY_DATA", os.path.join(HERE, "..", "foody_data"))
EP = os.environ.get("AQD_FOODY_IMPORT_EP", "/v1/admin/imports/restaurants")
DRINK_HINT = ("trà", "cà phê", "cafe", "coffee", "sinh tố", "nước", "soda", "bia", "sữa", "juice", "tea", "đá xay")
ROLE_MAP = {"food": "food", "drink": "drink", "menu": "menu", "interior": "interior", "venue": "interior",
            "storefront": "storefront_or_sign", "signboard": "storefront_or_sign", "sign": "storefront_or_sign",
            "exterior": "storefront_or_sign", "storefront_or_sign": "storefront_or_sign"}


def _price(v):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def payload(p):
    menu = []
    for m in p.get("menu") or []:
        price = _price(m.get("price"))
        if not m.get("name") or price is None:  # server không nhận giá 0/rỗng
            continue
        txt = ((m.get("category") or "") + " " + m["name"]).lower()
        menu.append({"category": m.get("category"), "name": m["name"], "price": price,
                     "kind": "drink" if any(h in txt for h in DRINK_HINT) else "food"})
    per = [x for x in p.get("opening_periods") or []
           if isinstance(x.get("weekday"), int) and 1 <= x["weekday"] <= 7 and x.get("opens_at")]
    body = {"source": "foody", "source_id": str(p["source_id"]), "source_url": p.get("source_url"),
            "name": p.get("name"), "address": p.get("address_full") or p.get("address"),
            "district": p.get("district"), "city": p.get("city"), "lat": p.get("lat"), "lng": p.get("lng"),
            "website": p.get("website"), "phones": p.get("phones") or [],
            "cuisines": p.get("cuisines") or [], "categories": p.get("categories") or [],
            "opening_periods": per, "menu": menu}
    return {k: v for k, v in body.items() if v is not None}


def problems(b):
    out = [k for k in ("name", "address", "source_id") if not str(b.get(k) or "").strip()]
    try:
        if not (-90 <= float(b["lat"]) <= 90 and -180 <= float(b["lng"]) <= 180):
            out.append("coords_range")
    except (KeyError, TypeError, ValueError):
        out.append("coords")
    return out


def reviews_of(p):
    out = []
    for x in p.get("reviews_sample") or []:
        if not x.get("source_id"):
            continue  # không có id nguồn thì không bao giờ gửi (không lọc/xoá được)
        out.append({"source": "foody", "source_id": str(x["source_id"]), "source_url": x.get("source_url"),
                    "author_name": x.get("author"), "rating": x.get("rating"), "text": x.get("text"),
                    "restaurant_source_id": str(p["source_id"]), "status": "pending_no_endpoint"})
    return out


def photos_of(p):
    out = []
    for x in p.get("photos") or []:
        role = ROLE_MAP.get(x.get("role") or "") or ROLE_MAP.get(x.get("album") or "")
        if x.get("keep") and x.get("file") and x.get("source_id") and role:
            out.append({**x, "_role": role})
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); ap.add_argument("--ids", nargs="*")
    a = ap.parse_args()
    ids = a.ids or sorted(d for d in os.listdir(ROOT) if os.path.exists(os.path.join(ROOT, d, "place.json")))
    log = os.path.join(ROOT, "_uploaded.json")
    done = json.load(open(log, encoding="utf-8")) if os.path.exists(log) else {}
    api = APIError = None
    if a.apply:
        from aqd_api import AQD, APIError
        api = AQD()
    prev = open(os.path.join(ROOT, "_upload_preview.jsonl"), "w", encoding="utf-8")
    revf = open(os.path.join(ROOT, "_reviews_pending.jsonl"), "w", encoding="utf-8")
    st = {"places": 0, "ok": 0, "bad": 0, "photos": 0, "reviews_pending": 0, "dup": 0, "existing": 0, "fail": 0}
    for fid in ids:
        p = json.load(open(os.path.join(ROOT, fid, "place.json"), encoding="utf-8"))
        body, ph, rv = payload(p), photos_of(p), reviews_of(p)
        st["places"] += 1; st["photos"] += len(ph); st["reviews_pending"] += len(rv)
        for r_ in rv:
            revf.write(json.dumps(r_, ensure_ascii=False) + "\n")
        bad = problems(body)
        prev.write(json.dumps({**body, "_problems": bad, "photos": [{"source_id": x["source_id"], "role": x["_role"]} for x in ph],
                               "reviews_pending": [r_["source_id"] for r_ in rv]}, ensure_ascii=False) + "\n")
        if bad:
            st["bad"] += 1; print(f"[bỏ] #{fid} thiếu {bad}"); continue
        st["ok"] += 1
        if not a.apply:
            print(f"[dry] #{fid} {body['name']} | {len(body['opening_periods'])} ca | {len(body['menu'])} món | {len(ph)} ảnh | {len(rv)} review chờ")
            continue
        rec = done.get(fid) or {}
        try:
            if not rec.get("restaurant_id"):
                r = api.call("POST", EP, json_body=body)
                cands = (r.get("duplicate") or {}).get("candidates") or []
                rec = {"restaurant_id": r["restaurant_id"], "created": r.get("created"),
                       "duplicate_suspected": bool((r.get("duplicate") or {}).get("suspected")),
                       "duplicate_of": [{"restaurant_id": c.get("restaurant_id"), "score": c.get("score"),
                                         "reasons": c.get("reasons")} for c in cands], "photos": []}
                done[fid] = rec; json.dump(done, open(log, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            rid = rec["restaurant_id"]; sent = set(rec.get("photos") or [])
            for x in ph:
                if x["source_id"] in sent:
                    continue
                path = os.path.join(ROOT, fid, x["file"])
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as f:
                    api.call("POST", f"{EP}/{rid}/photos",
                             data={"source": "foody", "source_id": x["source_id"], "source_url": x.get("source_url") or "",
                                   "role": x["_role"]},
                             files={"image": (os.path.basename(path), f, "image/jpeg")})
                rec.setdefault("photos", []).append(x["source_id"])
                json.dump(done, open(log, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except APIError as e:
            st["fail"] += 1; print(f"✗ #{fid}: HTTP {e.status} {e.code} {e.details or ''}"); continue
        if rec.get("duplicate_suspected"): st["dup"] += 1
        if rec.get("created") is False: st["existing"] += 1
        tag = " (quán đã import trước, cập nhật)" if rec.get("created") is False else ""
        if rec.get("duplicate_suspected"):
            tag += f" (NGHI TRÙNG: {[c['restaurant_id'] for c in rec['duplicate_of']]}, đã gắn cờ duplicate chờ admin)"
        print(f"✓ #{fid} -> quán {rid} | {len(rec.get('photos') or [])}/{len(ph)} ảnh{tag}")
    print("TỔNG", json.dumps(st, ensure_ascii=False))
    print("Đã gửi xong." if a.apply else "DRY-RUN: không gửi gì lên server. Thêm --apply để gửi thật.")


if __name__ == "__main__":
    main()
