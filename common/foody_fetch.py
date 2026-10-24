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
  --max-photos N  tối đa N ảnh mỗi quán (mặc định 5 = AQD_FOODY_MAX_PHOTOS): CHỈ ảnh thực đơn trước, rồi ảnh món
  --max-reviews N tối đa N review mỗi quán (mặc định 50 = AQD_MAX_REVIEWS), lật trang review đến khi đủ/hết
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
# predefineAlbumId của Foody -> vai trò ảnh bên mình. Thứ tự = thứ tự ưu tiên.
# an_uong_quanh_day#1082: CHỈ ảnh thực đơn (3) rồi ảnh món ăn/đồ uống (2). Không gian (1) / Tổng hợp (4) = ảnh quán,
# người, linh tinh -> không lấy nữa.
ALBUMS = {3: "menu", 2: "food"}
KEEP_ROLES = ("menu", "food")
MAX_PHOTOS = int(os.environ.get("AQD_FOODY_MAX_PHOTOS", "5"))
# an_uong_quanh_day#1083: up to this many reviews per place, paging Foody's review list until the cap or the end.
MAX_REVIEWS = int(os.environ.get("AQD_MAX_REVIEWS", "50"))
REVIEW_PAGE = 10  # what foody.vn's own "xem thêm" asks for
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PAGE_SIZE = 12  # HomeListPlace count=12

# an_uong_quanh_day#1080: an empty list page or one without Total is NOT the end of the list unless the page is past
# the last known total. Retry the same page with backoff, then skip it; never jump back to page 1 early.
LIST_RETRIES = int(os.environ.get("AQD_FOODY_LIST_RETRIES", "5"))
LIST_BACKOFF = float(os.environ.get("AQD_FOODY_LIST_BACKOFF", "30"))          # 30s, 60s, 120s ... capped
LIST_BACKOFF_MAX = float(os.environ.get("AQD_FOODY_LIST_BACKOFF_MAX", "600"))
LIST_SKIP_STREAK = int(os.environ.get("AQD_FOODY_LIST_SKIP_STREAK", "5"))     # this many skipped pages in a row ->
LIST_SKIP_PAUSE = float(os.environ.get("AQD_FOODY_LIST_SKIP_PAUSE", "1800"))  # long pause (Foody is throttling)
END_PAUSE = 600

# an_uong_quanh_day#1081: a gallery fetch that errors (503/busy, non-JSON, give-up) or comes back empty while the
# album counts say there are photos must not mark the place done; it is queued in _state.json "gallery_retry".
GALLERY_TRIES = int(os.environ.get("AQD_FOODY_GALLERY_TRIES", "5"))
GALLERY_BACKOFF = float(os.environ.get("AQD_FOODY_GALLERY_BACKOFF", "900"))    # 15 min, 30, 60 ... capped
GALLERY_BACKOFF_MAX = float(os.environ.get("AQD_FOODY_GALLERY_BACKOFF_MAX", "21600"))
GALLERY_RETRY_PER_PAGE = int(os.environ.get("AQD_FOODY_GALLERY_RETRY_PER_PAGE", "3"))  # retries done before each list page


def _make_heartbeat():
    """Crawler heartbeat for the daily Slack ops report (an_uong_quanh_day#1021). Token: env AQD_INGEST_TOKEN or the
    AQD_INGEST_TOKEN line of <AQD_ROOT>/creds.env; none -> no-op. Best effort, never stops the crawl."""
    try:
        import heartbeat
        aqd_root = os.environ.get("AQD_ROOT") or ("C:/aqd" if os.name == "nt" else os.path.expanduser("~/aqd"))
        return heartbeat.Heartbeat("script_foody", ROOT, env_file=os.path.join(aqd_root, "creds.env"))
    except Exception:
        return None


HB = None


def hb(method, *args, **kwargs):
    try:
        return getattr(HB, method)(*args, **kwargs) if HB else None
    except Exception:
        return None


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
                hb("note", "rate_limited", f"Foody {r.status_code} busy, backoff {back}s")
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


# Foody's ListRestaurantPicturesWithPaging counts pageIndex from 0: pageIndex=1 is the SECOND page (photos 21-40),
# so a place with fewer than 20 photos in an album came back empty ({'data': [], 'total': N}). That is how
# ~870 places went up with 0 photos (an_uong_quanh_day#1081, verified live 24/10/2026).
GALLERY_FIRST_PAGE = 0


class GalleryError(Exception):
    """The photo gallery could not be read reliably; the place stays retryable (#1081)."""


def _album_counts(albums):
    out = {}
    for k, v in (albums or {}).items():
        try:
            out[int(k)] = int(v or 0)
        except (TypeError, ValueError):
            pass
    return out


def photos(c, fid, ref, max_n, expected=None):
    """Menu photos first, then dish (food/drink) photos, max_n in total (#1082). No venue/people/other albums.
    expected = album counts from the place page ({predefineAlbumId: count}). Raises GalleryError when an album that
    was needed and should hold photos errors out (busy after retries, non-JSON, 404), or when both albums come back
    empty although the counts say they have photos (#1081)."""
    out, errors = [], []
    exp = _album_counts(expected)
    if max_n <= 0:
        return []
    for album, role in ALBUMS.items():
        if len(out) >= max_n:
            break
        if exp and exp.get(album, 0) <= 0:
            continue  # the place page says this album is empty: no request
        page, size = GALLERY_FIRST_PAGE, 20
        while len(out) < max_n:
            try:
                d = c.json(f"/__get/Gallery/ListRestaurantPicturesWithPaging?pageIndex={page}&pageSize={size}&restaurantId={fid}"
                           f"&predefineAlbumId={album}&albumId=0&userId=0&orderAscByTime=1&isProfessional=false",
                           referer=ref + "/album-anh")
            except Exception as e:  # Client.get gave up (busy x6) or HTTP error
                errors.append((album, f"{type(e).__name__}: {str(e)[:80]}")); break
            if not isinstance(d, dict):
                errors.append((album, "no JSON")); break
            rows = d.get("data") or []
            for p in rows:
                if p.get("MediaType") not in (None, 0, 1) or not p.get("FullSizeImageUrl"):
                    continue
                out.append({"foody_photo_id": p.get("Id"), "url": p["FullSizeImageUrl"], "album": role,
                            "width": (p.get("Size") or {}).get("Width"), "height": (p.get("Size") or {}).get("Height"),
                            "owner": p.get("OwnerFullName")})
                if len(out) >= max_n:
                    break
            if len(rows) < size:
                break
            page += 1
    bad = [(a, e) for a, e in errors if exp.get(a, 1) > 0]
    if bad:
        raise GalleryError("gallery error: " + "; ".join(f"album {a}: {e}" for a, e in bad))
    want = sum(exp.get(a, 0) for a in ALBUMS)
    if want > 0 and not out:
        raise GalleryError(f"gallery empty but album counts say {want} menu/dish photos")
    return filter_photos(out, max_n)


def filter_photos(items, max_n=None):
    """#1082: keep ONLY menu and dish photos, menu first, then dishes, at most max_n (default AQD_FOODY_MAX_PHOTOS).
    Returns the kept photos (each keep=True) in that order; venue/people/other photos are dropped."""
    max_n = MAX_PHOTOS if max_n is None else max_n
    rank = {r: i for i, r in enumerate(KEEP_ROLES)}
    keep = sorted((it for it in items if it.get("album") in rank), key=lambda it: rank[it["album"]])  # stable
    keep = keep[:max(0, max_n)]
    for it in keep:
        it["keep"] = True
    return keep


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


def _foody_date(x):
    m = re.search(r"/Date\((-?\d+)", str(x or ""))
    if not m:
        return x or None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).isoformat(timespec="seconds")


def review_row(x):
    """One item of /__get/Review/ResLoadMore -> the place.json review shape (same keys as the list-item sample)."""
    rid = x.get("Id")
    return {"source": "foody", "source_id": str(rid) if rid else None, "imported_from": "foody",
            "rating": x.get("AvgRating"), "title": x.get("Title"), "text": x.get("Description"),
            "author": (x.get("Owner") or {}).get("DisplayName"), "created_at": _foody_date(x.get("CreatedDate")),
            "source_url": BASE + x["Url"] if x.get("Url") else None}


def sample_rows(item):
    return [{"source": "foody", "source_id": _review_id(x), "imported_from": "foody",
             "rating": x.get("AvgRating"), "text": x.get("Comment"),
             "author": x.get("reviewUserDisplayName"), "created_at": x.get("CreatedOn"),
             "source_url": BASE + (x.get("reviewUrl") or "")}
            for x in item.get("LstReview") or []]


def reviews(c, fid, ref, cap, seed=()):
    """#1083: newest reviews of the place, paging Foody's review list (REVIEW_PAGE per request) until `cap` or the
    source runs out; the list item's LstReview sample (`seed`) fills in anything paging did not return.
    Best effort: a failing page keeps what was read so far (reviews never block a place)."""
    out, seen = [], set()

    def add(r):
        if r.get("source_id") and r["source_id"] not in seen and len(out) < cap:
            seen.add(r["source_id"]); out.append(r)

    last, pages = "", 0
    while cap > 0 and len(out) < cap and pages < cap // REVIEW_PAGE + 3:
        pages += 1
        try:
            d = c.json(f"/__get/Review/ResLoadMore?t={int(time.time() * 1000)}&ResId={fid}&Count={REVIEW_PAGE}"
                       f"&LastId={last}&Type=1&isLatest=true&ExcludeIds=", referer=ref + "/binh-luan")
        except Exception as e:
            print(f"  ! review #{fid} trang {pages}: {type(e).__name__} -> giữ {len(out)} review", flush=True); break
        rows = (d or {}).get("Items") or [] if isinstance(d, dict) else []
        before = len(out)
        for x in rows:
            add(review_row(x))
        nxt = str(d.get("LastId") or "") if isinstance(d, dict) else ""
        if len(rows) < REVIEW_PAGE or not nxt or nxt == last or len(out) == before:
            break
        last = nxt
    for r in seed:
        add(r)
    return out


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
    ph = photos(c, fid, url, args.max_photos, expected=det.get("albums")) if not args.no_photos else []
    if ph:
        download(c, ph, os.path.join(ROOT, str(fid), "photos"))
    rv = reviews(c, fid, url, getattr(args, "max_reviews", MAX_REVIEWS), seed=sample_rows(item))
    rec = {
        "source": "foody", "source_id": str(fid), "source_url": ref, "fetched_at": now(),
        **det,
        "list_phone": item.get("Phone"),
        "phones": phones,
        "rating": det.get("rating") or item.get("AvgRating"),
        "review_count": det.get("review_count") or item.get("TotalReviews"),
        "opening_hours": days, "opening_periods": periods,
        "menu": mn,
        # Mọi review Foody gắn source='foody' + source_id để xoá hàng loạt (issue #682).
        # Tên khoá giữ "reviews_sample" (foody_upload/foody_push đọc khoá này) nhưng giờ là tối đa AQD_MAX_REVIEWS review.
        "reviews_sample": rv,
        "photos": [{**p, "source": "foody", "source_id": str(p.get("foody_photo_id")),
                    "source_url": p["url"]} for p in ph],
    }
    rec["address_full"] = item.get("Address") or rec.get("address")
    return rec


def plan_empty_page(st, page, total, page_size=PAGE_SIZE, retries=None, backoff=None, backoff_max=None,
                    skip_streak=None):
    """What to do with an EMPTY list page (#1080). Mutates st (list_retry, skip_streak, skipped_pages, total).
    Returns (action, next_page, wait_seconds):
      "end"   - genuinely past the last known total (or no total was ever seen): pause, then page 1
      "retry" - same page again after wait (Total missing / page still below the known total)
      "skip"  - retries used up: move on to page+1 (wait > 0 = long pause first, Foody keeps refusing)"""
    retries = LIST_RETRIES if retries is None else retries
    backoff = LIST_BACKOFF if backoff is None else backoff
    backoff_max = LIST_BACKOFF_MAX if backoff_max is None else backoff_max
    skip_streak = LIST_SKIP_STREAK if skip_streak is None else skip_streak
    if isinstance(total, int) and total > 0:
        st["total"] = total
    known = st.get("total")
    if not known or (page - 1) * page_size >= int(known):
        st["list_retry"] = 0; st["skip_streak"] = 0
        return "end", 1, END_PAUSE
    n = int(st.get("list_retry") or 0) + 1
    if n <= retries:
        st["list_retry"] = n
        return "retry", page, min(backoff * (2 ** (n - 1)), backoff_max)
    st["list_retry"] = 0
    st["skipped_pages"] = (list(st.get("skipped_pages") or []) + [page])[-200:]
    streak = int(st.get("skip_streak") or 0) + 1
    if streak >= skip_streak:
        st["skip_streak"] = 0
        return "skip", page + 1, LIST_SKIP_PAUSE
    st["skip_streak"] = streak
    return "skip", page + 1, 0


def note_page_ok(st, total):
    if isinstance(total, int) and total > 0:
        st["total"] = total
    st["list_retry"] = 0; st["skip_streak"] = 0


def _slim_item(it):
    """What fetch_place needs from a list item, kept in _state.json for a gallery retry."""
    return {k: it.get(k) for k in ("Id", "Url", "Phone", "Address", "AvgRating", "TotalReviews", "LstReview")}


def queue_gallery_retry(st, fid, item, err, now_ts=None, tries=None, backoff=None, backoff_max=None):
    """#1081: keep the place out of "done" and schedule another full fetch with backoff. Returns the entry.
    After `tries` failures it moves to st["gallery_failed"] (still NOT done, so the next list pass picks it up)."""
    tries = GALLERY_TRIES if tries is None else tries
    backoff = GALLERY_BACKOFF if backoff is None else backoff
    backoff_max = GALLERY_BACKOFF_MAX if backoff_max is None else backoff_max
    now_ts = time.time() if now_ts is None else now_ts
    q = st.setdefault("gallery_retry", {})
    e = q.get(fid) or {"tries": 0}
    e = {"tries": int(e.get("tries") or 0) + 1, "item": _slim_item(item), "error": str(err)[:200], "at": now()}
    st.get("done", {}).pop(fid, None)
    if e["tries"] >= tries:
        q.pop(fid, None)
        st.setdefault("gallery_failed", {})[fid] = {"tries": e["tries"], "error": e["error"], "at": e["at"]}
        return e
    e["next_at"] = now_ts + min(backoff * (2 ** (e["tries"] - 1)), backoff_max)
    q[fid] = e
    return e


def due_gallery_retries(st, now_ts=None, limit=GALLERY_RETRY_PER_PAGE):
    now_ts = time.time() if now_ts is None else now_ts
    q = st.get("gallery_retry") or {}
    due = sorted((e.get("next_at", 0), fid) for fid, e in q.items() if e.get("next_at", 0) <= now_ts)
    return [fid for _, fid in due[:limit]]


def process_item(c, it, a, st, pusher, retry=False):
    """Fetch one place, write place.json, hand it to the pusher. Returns "ok" | "none" | "gallery" | "error"."""
    fid = str(it["Id"])
    try:
        rec = fetch_place(c, it, a)
        if rec:
            d = os.path.join(ROOT, fid); os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, "place.json.tmp")
            json.dump(rec, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(d, "place.json"))
            k = sum(1 for p in rec["photos"] if p.get("keep"))
            print(f"  ✓ #{fid} {rec['name']} | {len(rec['opening_periods'])} ca | {len(rec['menu'])} món | ảnh {k}/{len(rec['photos'])}"
                  + (" (gallery retry ok)" if retry else ""), flush=True)
            if pusher:
                pusher.append_reviews(fid, rec); pusher.q.put(fid)
            hb("record_place", True, photos=k, dishes=len(rec["menu"]))
        st["done"][fid] = now()
        (st.get("gallery_retry") or {}).pop(fid, None); (st.get("gallery_failed") or {}).pop(fid, None)
        return "ok" if rec else "none"
    except GalleryError as e:
        ent = queue_gallery_retry(st, fid, it, e)
        when = (f"thử lại sau {int(ent['next_at'] - time.time())}s" if "next_at" in ent
                else "hết lượt, chờ vòng danh sách sau")
        print(f"  ↻ #{fid}: {e} -> CHƯA xong, {when} (lần {ent['tries']}/{GALLERY_TRIES})", flush=True)
        hb("record_place", False); hb("note", "rate_limited", f"#{fid}: {str(e)[:120]}")
        return "gallery"
    except Exception as e:
        st["failed"][fid] = st["failed"].get(fid, 0) + 1
        print(f"  ✗ #{fid}: {e}", flush=True)
        hb("record_place", False); hb("note", None, f"#{fid}: {type(e).__name__}: {str(e)[:120]}")
        if st["failed"][fid] >= 3:
            st["done"][fid] = "skip-3-fails"
        return "error"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--start-page", type=int, default=None)
    ap.add_argument("--max-photos", type=int, default=MAX_PHOTOS, help="AQD_FOODY_MAX_PHOTOS, mặc định 5 (menu trước rồi món)")
    ap.add_argument("--max-reviews", type=int, default=MAX_REVIEWS, help="AQD_MAX_REVIEWS, mặc định 50")
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
    global HB
    HB = _make_heartbeat()
    stp = os.path.join(ROOT, "_state.json")
    try:
        st = json.load(open(stp, encoding="utf-8")) if os.path.exists(stp) else {"page": 1, "done": {}, "failed": {}}
    except Exception as e:
        print(f"[warn] _state.json hỏng ({e}) -> dùng bản .bak nếu có", flush=True)
        st = json.load(open(stp + ".bak", encoding="utf-8")) if os.path.exists(stp + ".bak") else {"page": 1, "done": {}, "failed": {}}
    st.setdefault("done", {}); st.setdefault("failed", {})
    pusher = None
    if not a.no_push:
        from foody_push import Pusher, free_gb
        pusher = Pusher().start()
    page = a.start_page or st.get("page", 1)
    c, n = Client(), 0
    print(f"parity={a.parity} page={page} known_total={st.get('total')}", flush=True)
    while a.limit == 0 or n < a.limit:
        if os.path.exists(os.path.join(ROOT, "STOP")):
            print("thấy file STOP -> dừng cào" + (", đẩy nốt backlog rồi thoát" if pusher else ""), flush=True)
            if pusher: pusher.drain_and_stop(); pusher.t.join()
            hb("beat", "dừng (STOP)", status="idle", force=True)
            break
        if pusher and free_gb() < a.min_free_gb:
            print(f"ổ còn {free_gb():.1f} GB < {a.min_free_gb} GB -> tạm ngừng cào 5 phút, uploader đang xoá ảnh đã lên", flush=True)
            hb("note", "disk_full", f"disk {free_gb():.1f} GB < {a.min_free_gb} GB")
            hb("beat", "tạm ngừng: ổ đầy", status="blocked", force=True)
            time.sleep(300); continue
        for fid in due_gallery_retries(st):  # #1081: places whose gallery failed earlier
            ent = st["gallery_retry"][fid]
            print(f"  ↻ thử lại ảnh #{fid} (lần {ent['tries'] + 1})", flush=True)
            if process_item(c, ent["item"], a, st, pusher, retry=True) == "ok":
                n += 1
            _save_state(st, stp)
        items, total = list_page(c, page)
        print(f"trang {page}: {len(items)} quán (tổng {total})", flush=True)
        if not items:
            action, nxt, wait = plan_empty_page(st, page, total)
            if action == "end":
                hb("beat", "hết danh sách, nghỉ 10 phút", status="idle")
                print(f"hết danh sách (trang {page} > tổng {st.get('total')}), nghỉ {int(wait // 60)} phút rồi quét lại từ trang 1", flush=True)
            elif action == "retry":
                hb("note", "rate_limited", f"Foody list page {page} empty (Total {total}); retry {st['list_retry']}/{LIST_RETRIES}")
                print(f"trang {page} trống/không có tổng (đã biết tổng {st.get('total')}) -> thử lại sau {int(wait)}s "
                      f"(lần {st['list_retry']}/{LIST_RETRIES}), KHÔNG quay về trang 1", flush=True)
            else:
                print(f"trang {page} vẫn trống sau {LIST_RETRIES} lần -> bỏ qua, sang trang {nxt}"
                      + (f"; nhiều trang liền bị bỏ, nghỉ {int(wait // 60)} phút" if wait else ""), flush=True)
            page = nxt; st["page"] = page; _save_state(st, stp)
            if wait: time.sleep(wait)
            continue
        note_page_ok(st, total)
        for it in items:
            if a.limit and n >= a.limit:
                break
            fid = str(it["Id"])
            if a.parity != "all" and (int(fid) % 2 == 1) != (a.parity == "odd"):
                continue
            if fid in st["done"] and not a.refetch:
                continue
            if fid in (st.get("gallery_retry") or {}):
                continue  # waits for its own backoff slot
            if process_item(c, it, a, st, pusher) in ("ok", "none"):
                n += 1
            _save_state(st, stp)
            hb("beat", f"trang {page}: #{fid}", range_label=f"trang {page}")
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
