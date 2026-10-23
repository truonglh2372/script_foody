"""Lấy dữ liệu quán từ Foody (chỉ để TEST, sẽ xoá sau).

Chỉ dùng trang/JSON công khai mà Foody trả cho người xem bình thường:
không đăng nhập, không vượt captcha. Mỗi request cách nhau >= AQD_FOODY_DELAY giây
(mặc định 2s), lùi dần khi gặp 429/403/"System Busy". Có thể dừng/chạy lại (resumable).

Ảnh được tải NGUYÊN BẢN, không cắt/che/xoá watermark.

Dùng:
  python common/foody_fetch.py --limit 20            # lấy 20 quán đầu danh sách HCM
  python common/foody_fetch.py --limit 0             # chạy hết (vòng lặp vô hạn trang)
  python common/foody_fetch.py --ids 30086 12345     # chỉ các id này (cần url trong state)
  --no-photos     chỉ lấy JSON, không tải ảnh
  --max-photos N  tối đa N ảnh mỗi quán (mặc định 30)
Kết quả: foody_data/<foody_id>/place.json + photos/*.jpg ; tiến độ foody_data/_state.json
"""
from __future__ import annotations
import argparse, html, json, os, random, re, sys, time, urllib.parse
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from datetime import datetime, timezone
import requests

BASE = "https://www.foody.vn"
CITY = os.environ.get("AQD_FOODY_CITY", "ho-chi-minh")
CITY_ID = int(os.environ.get("AQD_FOODY_CITY_ID", "217"))
DELAY = float(os.environ.get("AQD_FOODY_DELAY", "2.0"))
ROOT = os.environ.get("AQD_FOODY_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "foody_data"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
# predefineAlbumId của Foody -> vai trò ảnh bên mình
ALBUMS = {3: "menu", 2: "food", 1: "interior", 4: "other"}
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Client:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "vi-VN,vi;q=0.9"})
        self.last = 0.0
        self.warm = False

    def get(self, path, referer=None, xhr=False, tries=6, raw=False):
        url = path if path.startswith("http") else BASE + path
        if not self.warm and not raw:
            self.warm = True
            self.get(f"/{CITY}/o-dau")
        h = {}
        if referer:
            h["Referer"] = referer if referer.startswith("http") else BASE + referer
        if xhr:
            h.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"})
        back = 30
        for i in range(tries):
            wait = DELAY + random.uniform(0, DELAY / 2) - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.get(url, headers=h, timeout=40)
            except requests.RequestException as e:
                print(f"  ! {e.__class__.__name__} {url} -> chờ {back}s", flush=True)
                time.sleep(back); back = min(back * 2, 600); continue
            busy = r.status_code in (429, 403, 503) or (
                not raw and "text/html" in r.headers.get("content-type", "") and "System Busy" in r.text[:600])
            if busy:
                print(f"  ! {r.status_code} busy {url} -> chờ {back}s", flush=True)
                time.sleep(back); back = min(back * 2, 600); continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        raise RuntimeError(f"bỏ cuộc sau {tries} lần: {url}")

    def json(self, path, referer=None):
        r = self.get(path, referer=referer, xhr=True)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            return None


# ---------- danh sách ----------
def list_page(c, page, count=12):
    q = (f"/__get/Place/HomeListPlace?t={int(time.time()*1000)}&page={page}&lat=10.823099&lon=106.629664"
         f"&count={count}&districtId=&cateId=&cuisineId=&isReputation=&type=1")
    d = c.json(q, referer=f"/{CITY}/o-dau") or {}
    return d.get("Items") or [], d.get("Total")


# ---------- chi tiết ----------
def _var(s, name):
    m = re.search(r"var\s+" + name + r"\s*=\s*(\{.*?\});\s*\n", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _txt(x):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x or ""))).strip()


def parse_detail(s):
    d = _var(s, "initData") or {}
    nav = _var(s, "initDataNavBar") or {}
    out = {
        "name": d.get("Name"), "address": d.get("Address"), "district": d.get("District"),
        "area": d.get("Area"), "city": d.get("City"),
        "lat": d.get("Latitude"), "lng": d.get("Longtitude"),
        "phone": d.get("Phone"), "website": d.get("Website") or None,
        "price_min": d.get("PriceMin"), "price_max": d.get("PriceMax"),
        "cuisines": [x.get("Name") for x in d.get("Cuisines") or []],
        "rating": None, "review_count": d.get("TotalReview"),
        "rating_breakdown": {p["Label"]: p["Point"] for p in d.get("AvgPointList") or [] if "Label" in p},
        "total_pictures": d.get("TotalPictures"), "description": d.get("SpecialDesc"),
        "cover_url": (d.get("PictureModel") or {}).get("FullSizeImageUrl"),
        "brand": d.get("BrandName"), "is_delivery": nav.get("IsDelivery"),
        "albums": {a["Id"]: a.get("Counts", 0) for a in nav.get("PredefineAlbums") or []},
    }
    m = re.search(r'itemprop="ratingValue"[^>]*>\s*([\d.]+)', s)
    if m:
        out["rating"] = float(m.group(1))
    cats = re.findall(r'class="category-items"[^>]*>\s*<a[^>]*title="([^"]+)"', s)
    out["categories"] = [html.unescape(x) for x in cats]
    aud = re.search(r'class="audiences"[^>]*>(.{0,600}?)</div>', s, re.S)
    out["audiences"] = [x.strip() for x in _txt(aud.group(1)).lstrip("- ").split(",") if x.strip()] if aud else []
    return out


def opening(c, fid, ref):
    d = c.json(f"/__get/Restaurant/GetOpeningTime?resId={fid}", referer=ref) or {}
    days, periods = [], []
    for it in d.get("Items") or []:
        dow = it.get("DOW")  # 0=CN
        wd = 7 if dow == 0 else dow  # 1=T2..7=CN như script_google_map
        times = [(t.get("TimeOpen"), t.get("TimeClose")) for t in it.get("Times") or []]
        days.append({"day": it.get("DayOfWeek"), "weekday": wd, "day_off": it.get("IsDayOff"),
                     "times": [f"{a}-{b}" for a, b in times]})
        if not it.get("IsDayOff"):
            periods += [{"weekday": wd, "opens_at": a, "closes_at": b} for a, b in times]
    return days, sorted(periods, key=lambda p: (p["weekday"], p["opens_at"] or ""))


def phone(c, fid, ref):
    r = c.get(f"/__get/Restaurant/RestaurantPhone?t={int(time.time()*1000)}&resId={fid}", referer=ref, xhr=True)
    if r is None:
        return []
    nums = re.findall(r"(?:\+?84|0)[\d .]{8,13}\d", _txt(r.text))
    return sorted({re.sub(r"\D", "", n) for n in nums if 9 <= len(re.sub(r"\D", "", n)) <= 12})


def menu(c, fid, ref, cap=500):
    """Menu công khai: JSON window.intMenu trong trang /thuc-don (nhóm -> Dishes)."""
    r = c.get(ref + "/thuc-don", referer=ref)
    if r is None:
        return []
    m = re.search(r"window\.intMenu\s*=\s*(\{.*?\});\s*\n", r.text, re.S)
    try:
        d = json.loads(m.group(1)) if m else {}
    except ValueError:
        d = {}
    items = []
    for grp in d.get("MenuItems") or []:
        for x in grp.get("Dishes") or []:
            items.append({"name": x.get("Name"), "price": x.get("Price"), "category": grp.get("Name"),
                          "description": x.get("Description"),
                          "photo_url": x.get("ImageUrl") or x.get("ImageThumbUrl") or x.get("RepresentImg")})
    return items[:cap]


def photos(c, fid, ref, max_n):
    out = []
    for album, role in ALBUMS.items():
        page = 1
        while len(out) < max_n:
            d = c.json(f"/__get/Gallery/ListRestaurantPicturesWithPaging?pageIndex={page}&pageSize=20&restaurantId={fid}"
                       f"&predefineAlbumId={album}&albumId=0&userId=0&orderAscByTime=1&isProfessional=false",
                       referer=ref + "/album-anh") or {}
            rows = d.get("data") or []
            for p in rows:
                if p.get("MediaType") not in (None, 0, 1) or not p.get("FullSizeImageUrl"):
                    continue
                out.append({"foody_photo_id": p.get("Id"), "url": p["FullSizeImageUrl"], "album": role,
                            "width": (p.get("Size") or {}).get("Width"), "height": (p.get("Size") or {}).get("Height"),
                            "owner": p.get("OwnerFullName")})
            if len(rows) < 20:
                break
            page += 1
    return out[:max_n]


def filter_photos(items):
    """#855: no local model. Keep every photo of the Thực đơn / Món ăn / Không gian albums; the API classifies
    each upload. 'Tổng hợp' (album other) has no import role the API accepts yet, so it stays local only."""
    for it in items:
        it["keep"] = it["album"] != "other"
    return items


def download(c, items, folder):
    os.makedirs(folder, exist_ok=True)
    for it in items:
        if not it.get("keep"):
            continue
        fn = os.path.join(folder, f"{it['foody_photo_id'] or abs(hash(it['url']))}.jpg")
        it["file"] = os.path.relpath(fn, os.path.dirname(folder))
        if os.path.exists(fn) and os.path.getsize(fn) > 0:
            continue
        r = c.get(it["url"], raw=True)  # ảnh gốc, giữ nguyên byte (KHÔNG xử lý watermark)
        if r is not None:
            with open(fn, "wb") as f:
                f.write(r.content)


def _review_id(x):
    rid = x.get("Id") or x.get("ReviewId") or x.get("reviewId")
    if not rid:
        m = re.search(r"(\d+)(?:\D*)$", x.get("reviewUrl") or "")
        rid = m.group(1) if m else None
    return str(rid) if rid else None


def fetch_place(c, item, args):
    fid, url = item["Id"], item["Url"]
    ref = BASE + url
    r = c.get(url, referer=f"/{CITY}/o-dau")
    if r is None:
        return None
    det = parse_detail(r.text)
    days, periods = opening(c, fid, url)
    phones = phone(c, fid, url) if not det.get("phone") else [re.sub(r"\D", "", det["phone"])]
    mn = menu(c, fid, url)
    ph = photos(c, fid, url, args.max_photos) if not args.no_photos else []
    if ph:
        filter_photos(ph)
        download(c, ph, os.path.join(ROOT, str(fid), "photos"))
    rec = {
        "source": "foody", "source_id": str(fid), "source_url": ref, "fetched_at": now(),
        **det,
        "list_phone": item.get("Phone"),
        "phones": phones,
        "rating": det.get("rating") or item.get("AvgRating"),
        "review_count": det.get("review_count") or item.get("TotalReviews"),
        "opening_hours": days, "opening_periods": periods,
        "menu": mn,
        # Mọi review Foody gắn source='foody' + source_id để xoá hàng loạt (issue #682)
        "reviews_sample": [{"source": "foody", "source_id": _review_id(x), "imported_from": "foody",
                            "rating": x.get("AvgRating"), "text": x.get("Comment"),
                            "author": x.get("reviewUserDisplayName"), "created_at": x.get("CreatedOn"),
                            "source_url": BASE + (x.get("reviewUrl") or "")}
                           for x in item.get("LstReview") or []],
        "photos": [{**p, "source": "foody", "source_id": str(p.get("foody_photo_id")),
                    "source_url": p["url"]} for p in ph],
    }
    rec["address_full"] = item.get("Address") or rec.get("address")
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--start-page", type=int, default=None)
    ap.add_argument("--max-photos", type=int, default=int(os.environ.get("AQD_FOODY_MAX_PHOTOS", "30")))
    ap.add_argument("--no-photos", action="store_true")
    ap.add_argument("--no-clip", action="store_true", help="bỏ (#855): không còn CLIP local, giữ để lệnh cũ không lỗi")
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--no-push", action="store_true", help="không đẩy lên server (chỉ cào)")
    ap.add_argument("--min-free-gb", type=float, default=float(os.environ.get("AQD_FOODY_MIN_FREE_GB", "3")),
                    help="ổ trống dưới mức này thì tạm ngừng cào, chờ uploader xoá ảnh")
    ap.add_argument("--parity", choices=["odd", "even", "all"],
                    default=os.environ.get("AQD_FOODY_PARITY", "all") or "all",
                    help="chia việc 2 máy theo Foody id: odd (Mac) / even (Admin)")
    a = ap.parse_args(argv)
    os.makedirs(ROOT, exist_ok=True)
    stp = os.path.join(ROOT, "_state.json")
    try:
        st = json.load(open(stp, encoding="utf-8")) if os.path.exists(stp) else {"page": 1, "done": {}, "failed": {}}
    except Exception as e:
        print(f"[warn] _state.json hỏng ({e}) -> dùng bản .bak nếu có", flush=True)
        st = json.load(open(stp + ".bak", encoding="utf-8")) if os.path.exists(stp + ".bak") else {"page": 1, "done": {}, "failed": {}}
    pusher = None
    if not a.no_push:
        from foody_push import Pusher, free_gb
        pusher = Pusher().start()
    page = a.start_page or st.get("page", 1)
    c, n = Client(), 0
    print(f"parity={a.parity}", flush=True)
    while a.limit == 0 or n < a.limit:
        if os.path.exists(os.path.join(ROOT, "STOP")):
            print("thấy file STOP -> dừng cào" + (", đẩy nốt backlog rồi thoát" if pusher else ""), flush=True)
            if pusher: pusher.drain_and_stop(); pusher.t.join()
            break
        if pusher and free_gb() < a.min_free_gb:
            print(f"ổ còn {free_gb():.1f} GB < {a.min_free_gb} GB -> tạm ngừng cào 5 phút, uploader đang xoá ảnh đã lên", flush=True)
            time.sleep(300); continue
        items, total = list_page(c, page)
        print(f"trang {page}: {len(items)} quán (tổng {total})", flush=True)
        if not items:
            print("hết danh sách, nghỉ 10 phút"); time.sleep(600); page = 1; continue
        for it in items:
            if a.limit and n >= a.limit:
                break
            fid = str(it["Id"])
            if a.parity != "all" and (int(fid) % 2 == 1) != (a.parity == "odd"):
                continue
            if fid in st["done"] and not a.refetch:
                continue
            try:
                rec = fetch_place(c, it, a)
                if rec:
                    d = os.path.join(ROOT, fid); os.makedirs(d, exist_ok=True)
                    tmp = os.path.join(d, "place.json.tmp")
                    json.dump(rec, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    os.replace(tmp, os.path.join(d, "place.json"))
                    k = sum(1 for p in rec["photos"] if p.get("keep"))
                    print(f"  ✓ #{fid} {rec['name']} | {len(rec['opening_periods'])} ca | {len(rec['menu'])} món | ảnh {k}/{len(rec['photos'])}", flush=True)
                    if pusher:
                        pusher.append_reviews(fid, rec); pusher.q.put(fid)
                st["done"][fid] = now(); n += 1
            except Exception as e:
                st["failed"][fid] = st["failed"].get(fid, 0) + 1
                print(f"  ✗ #{fid}: {e}", flush=True)
                if st["failed"][fid] >= 3:
                    st["done"][fid] = "skip-3-fails"
            _save_state(st, stp)
        page += 1; st["page"] = page
        _save_state(st, stp)
    print(f"xong: {n} quán")


def _save_state(st, stp):
    if os.path.exists(stp):
        try: os.replace(stp, stp + ".bak")
        except OSError: pass
    tmp = stp + ".tmp"; json.dump(st, open(tmp, "w", encoding="utf-8")); os.replace(tmp, stp)


if __name__ == "__main__":
    main()
