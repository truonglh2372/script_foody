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
# Multi-city: AQD_FOODY_CITIES=all (default) or comma slugs/ids. AQD_FOODY_CITY forces a single city when set
# without AQD_FOODY_CITIES. Discovery seed: data/foody_cities.json; verified cache: foody_data/_cities.json.
CITIES_ENV = os.environ.get("AQD_FOODY_CITIES")  # None = all (unless AQD_FOODY_CITY alone)
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
# Root cause of the endless "trang N: 0 quán": HomeListPlace returns at most LIST_CAP items per query (page*count
# <= 2000: count=12 -> page 166 is the last non-empty page, count=50 -> 40, count=100 -> 20) while Total still says
# 37208. Pages past the cap are ALWAYS empty. So the crawler treats min(Total, LIST_CAP) as the end of a query and
# walks the city in slices (districtId, and districtId+cateId when a district alone is over the cap).
LIST_CAP = int(os.environ.get("AQD_FOODY_LIST_CAP", "2000"))
SLICE_MAX_ID = int(os.environ.get("AQD_FOODY_SLICE_MAX_ID", "80"))  # probe districtId / cateId 1..this

# an_uong_quanh_day#1081: a gallery fetch that errors (503/busy, non-JSON, give-up) or comes back empty while the
# album counts say there are photos must not mark the place done; it is queued in _state.json "gallery_retry".
# A list page whose request hits this many ConnectTimeouts in a row is skipped (logged + Slack) instead of crashing
# the crawler; between timeouts wait AQD_FOODY_TIMEOUT_BACKOFF seconds (comma list, the last value repeats).
def _int_env(name, default, lo=1):
    try:
        return max(lo, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _backoff_env(name, default):
    try:
        vals = [float(x) for x in (os.environ.get(name, "") or default).split(",") if x.strip()]
        return [v for v in vals if v >= 0] or [float(x) for x in default.split(",")]
    except ValueError:
        return [float(x) for x in default.split(",")]


LIST_TIMEOUT_SKIP = _int_env("AQD_FOODY_LIST_TIMEOUT_SKIP", 6)
TIMEOUT_BACKOFF = _backoff_env("AQD_FOODY_TIMEOUT_BACKOFF", "60,120,300,600")  # 1, 2, 5, 10 min, then 10 min


def timeout_wait(n):
    """Seconds to wait after the n-th (1-based) consecutive ConnectTimeout."""
    return TIMEOUT_BACKOFF[min(n, len(TIMEOUT_BACKOFF)) - 1]


class ConnectTimeoutGiveUp(RuntimeError):
    """Raised by Client.get after LIST_TIMEOUT_SKIP ConnectTimeouts in a row on one URL."""

    def __init__(self, url, count):
        super().__init__(f"bỏ cuộc sau {count} lần ConnectTimeout liên tiếp: {url}")
        self.url, self.count = url, count


GALLERY_TRIES = int(os.environ.get("AQD_FOODY_GALLERY_TRIES", "5"))
GALLERY_BACKOFF = float(os.environ.get("AQD_FOODY_GALLERY_BACKOFF", "900"))    # 15 min, 30, 60 ... capped
GALLERY_BACKOFF_MAX = float(os.environ.get("AQD_FOODY_GALLERY_BACKOFF_MAX", "21600"))
GALLERY_RETRY_PER_PAGE = int(os.environ.get("AQD_FOODY_GALLERY_RETRY_PER_PAGE", "3"))  # retries done before each list page


def _make_notifier():
    """Slack status reports straight from the crawler (an_uong_quanh_day#1388): webhook from env
    SLACK_CRAWLER_WEBHOOK_URL or the local secret file (see README); none -> logs one warning, never stops the crawl."""
    try:
        import slack_notify
        return slack_notify.Notifier("foody", ROOT,
                                     version=slack_notify.git_version(os.path.dirname(os.path.abspath(__file__))))
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
    def __init__(self, city=None, city_id=None, lat=None, lng=None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "vi-VN,vi;q=0.9"})
        self.last = 0.0
        self.warm = False
        self.city = city or CITY
        self.city_id = int(city_id if city_id is not None else CITY_ID)
        self.lat = float(lat if lat is not None else 10.823099)
        self.lng = float(lng if lng is not None else 106.629664)

    def set_city(self, city):
        """Switch Foody province: cookie floc + /{slug}/o-dau warm. city is a dict from foody_cities."""
        self.city = city["slug"]
        self.city_id = int(city["id"])
        if city.get("lat") and city.get("lng"):
            self.lat, self.lng = float(city["lat"]), float(city["lng"])
        self.warm = False
        self.s.cookies.set("flg", "vn", domain="www.foody.vn")
        self.s.cookies.set("floc", str(self.city_id), domain="www.foody.vn")
        # Force a warm hit so Foody binds the session to this province.
        global CITY, CITY_ID
        CITY, CITY_ID = self.city, self.city_id
        self.get(f"/{self.city}/o-dau")
        self.warm = True
        print(f"[city] đang cào {self.city} (id={self.city_id}) lat={self.lat} lng={self.lng}", flush=True)

    def get(self, path, referer=None, xhr=False, tries=None, raw=False):
        tries = tries or max(6, LIST_TIMEOUT_SKIP)
        url = path if path.startswith("http") else BASE + path
        if not self.warm and not raw:
            self.warm = True
            self.get(f"/{self.city}/o-dau")
        h = {}
        if referer:
            h["Referer"] = referer if referer.startswith("http") else BASE + referer
        if xhr:
            h.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"})
        back = 30
        timeouts = 0
        for i in range(tries):
            wait = DELAY + random.uniform(0, DELAY / 2) - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.get(url, headers=h, timeout=40)
            except requests.ConnectTimeout as e:
                timeouts += 1
                if timeouts >= LIST_TIMEOUT_SKIP:
                    raise ConnectTimeoutGiveUp(url, timeouts) from e
                w = timeout_wait(timeouts)
                print(f"  ! ConnectTimeout {url} (lần {timeouts}/{LIST_TIMEOUT_SKIP}) -> chờ {int(w)}s", flush=True)
                time.sleep(w); continue
            except requests.RequestException as e:
                timeouts = 0
                print(f"  ! {e.__class__.__name__} {url} -> chờ {back}s", flush=True)
                time.sleep(back); back = min(back * 2, 600); continue
            timeouts = 0
            busy = r.status_code in (429, 403, 503) or (
                not raw and "text/html" in r.headers.get("content-type", "") and "System Busy" in r.text[:600])
            if busy:
                print(f"  ! {r.status_code} busy {url} -> chờ {back}s", flush=True)
                hb("error", "http_503" if r.status_code == 503 else "rate_limited",
                   f"Foody {r.status_code} busy, chờ {back}s")
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
def list_page(c, page, count=12, district="", cate=""):
    lat, lon = getattr(c, "lat", 10.823099), getattr(c, "lng", 106.629664)
    city = getattr(c, "city", CITY)
    q = (f"/__get/Place/HomeListPlace?t={int(time.time()*1000)}&page={page}&lat={lat}&lon={lon}"
         f"&count={count}&districtId={district}&cateId={cate}&cuisineId=&isReputation=&type=1")
    d = c.json(q, referer=f"/{city}/o-dau") or {}
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


def parse_int_menu(text):
    """window.intMenu = {...} -> dict. Đọc bằng JSONDecoder.raw_decode từ dấu { đầu tiên nên không phụ thuộc vào
    ký tự sau object (regex cũ đòi `};` + xuống dòng: trang gộp/minify `};</script>` hoặc `}; window.x=` -> menu rỗng,
    #7 mục 5). None nếu không có hoặc không phải JSON."""
    m = re.search(r"window\.intMenu\s*=\s*", text or "")
    if not m:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(text, m.end())
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def menu(c, fid, ref, cap=500):
    """Menu công khai: JSON window.intMenu trong trang /thuc-don (nhóm -> Dishes)."""
    r = c.get(ref + "/thuc-don", referer=ref)
    if r is None:
        return []
    d = parse_int_menu(r.text)
    if d is None:
        print(f"  ! menu #{fid}: trang /thuc-don không có window.intMenu đọc được ({len(r.text)} byte)", flush=True)
        d = {}
    items = []
    for grp in d.get("MenuItems") or d.get("Categories") or d.get("Groups") or []:
        if not isinstance(grp, dict):
            continue
        for x in grp.get("Dishes") or grp.get("Items") or []:
            if not isinstance(x, dict):
                continue
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
    if not known or (page - 1) * page_size >= min(int(known), LIST_CAP // page_size * page_size):  # count=12 -> 166 pages
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


def fetch_district_ids(c):
    """Foody district ids for the client's current province via GetDistrictsByProvinceId.
    These ids are global (HCM≈1..19, Hà Nội≈20..29+690+, Bắc Kạn≈198+), NOT 1..N per city.
    Returns a list of int ids, or [] on failure (caller may fall back to probing)."""
    cid = int(getattr(c, "city_id", CITY_ID) or CITY_ID)
    path = f"/__get/Common/GetDistrictsByProvinceId?provinceId={cid}&t={int(time.time() * 1000)}"
    try:
        data = c.json(path, referer=f"/{getattr(c, 'city', CITY)}/o-dau")
    except Exception as e:
        print(f"  ! GetDistrictsByProvinceId provinceId={cid}: {type(e).__name__}: {e}", flush=True)
        return []
    rows = (data or {}).get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        try:
            did = int(row.get("Id") if isinstance(row, dict) else row)
        except (TypeError, ValueError):
            continue
        if did > 0:
            out.append(did)
    # stable unique order as Foody returned
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d); uniq.append(d)
    return uniq


def build_slices(total_of, cap=None, max_id=None, district_ids=None):
    """Slices of the city list that each fit under the API cap. total_of(district, cate) -> Total (int or None).
    Districts under the cap are one slice; a district over it is split by every cateId seen city-wide.
    Prefer `district_ids` from GetDistrictsByProvinceId; otherwise probe 1..max_id (HCM-only legacy)."""
    cap = LIST_CAP if cap is None else cap
    max_id = SLICE_MAX_ID if max_id is None else max_id
    if district_ids:
        districts = [int(d) for d in district_ids if int(d) > 0]
    else:
        districts = list(range(1, max_id + 1))
    cates = [k for k in range(1, max_id + 1) if (total_of("", k) or 0) > 0]
    out = []
    for d in districts:
        t = total_of(d, "") or 0
        if t <= 0:
            continue
        if t <= cap or not cates:
            out.append({"district": d, "cate": "", "total": t})
            continue
        for k in cates:
            tk = total_of(d, k) or 0
            if tk > 0:
                out.append({"district": d, "cate": k, "total": tk})
    return out


def slice_kw(st):
    i = int(st.get("slice") or 0)
    sl = st.get("slices") or []
    if i <= 0 or i > len(sl):
        return {}
    return {"district": sl[i - 1]["district"], "cate": sl[i - 1]["cate"]}


def next_slice(st, build):
    """Called when the current query is exhausted. Returns True if a new slice starts (page 1, no pause),
    False when the whole sweep is done (pause, then start over from the unfiltered list)."""
    i = int(st.get("slice") or 0)
    if i == 0:
        st["slices"] = build()
    st.pop("total", None)
    if i + 1 <= len(st.get("slices") or []):
        st["slice"] = i + 1
        return True
    st["slice"] = 0
    return False


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
        hb("record_place", False); hb("error", "rate_limited", f"#{fid}: {str(e)[:120]}")
        return "gallery"
    except Exception as e:
        st["failed"][fid] = st["failed"].get(fid, 0) + 1
        print(f"  ✗ #{fid}: {e}", flush=True)
        hb("record_place", False)
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
    ap.add_argument("--cities", default=None,
                    help="all (mặc định) hoặc danh sách slug/id cách nhau bởi dấu phẩy; ghi đè AQD_FOODY_CITIES")
    ap.add_argument("--skip-cities", default=None,
                    help="slug/id cách nhau bởi dấu phẩy để BỎ QUA trong lượt này (giữ state); "
                         "ghi đè/ghép với AQD_FOODY_SKIP_CITIES và st['skip_cities']")
    a = ap.parse_args(argv)
    os.makedirs(ROOT, exist_ok=True)
    global HB
    HB = _make_notifier()
    stp = os.path.join(ROOT, "_state.json")
    try:
        st = json.load(open(stp, encoding="utf-8")) if os.path.exists(stp) else {"page": 1, "done": {}, "failed": {}}
    except Exception as e:
        print(f"[warn] _state.json hỏng ({e}) -> dùng bản .bak nếu có", flush=True)
        st = json.load(open(stp + ".bak", encoding="utf-8")) if os.path.exists(stp + ".bak") else {"page": 1, "done": {}, "failed": {}}
    import foody_cities as cities_mod
    st = cities_mod.migrate_state(st, default_slug=CITY)
    # City selection: --cities / AQD_FOODY_CITIES / default all. AQD_FOODY_SINGLE_CITY=1 keeps legacy one-city mode.
    if getattr(a, "cities", None) is not None:
        wanted = a.cities
    elif CITIES_ENV is not None:
        wanted = CITIES_ENV
    elif os.environ.get("AQD_FOODY_SINGLE_CITY", "").strip().lower() in ("1", "true", "yes"):
        wanted = os.environ.get("AQD_FOODY_CITY", CITY)
    else:
        wanted = "all"
    verify = os.environ.get("AQD_FOODY_VERIFY_CITIES", "0").strip().lower() not in ("0", "false", "no", "")
    # Default verify off so install/restart is fast; first run can set AQD_FOODY_VERIFY_CITIES=1.
    city_rows = cities_mod.discover_cities(root=ROOT, verify=verify)
    city_rows = cities_mod.filter_cities(city_rows, wanted)
    if not city_rows:
        raise SystemExit("không có city nào để cào (data/foody_cities.json rỗng hoặc filter loại hết)")
    order = [c["slug"] for c in city_rows]
    by_slug = {c["slug"]: c for c in city_rows}
    prev = [s for s in (st.get("city_order") or []) if s in by_slug]
    st["city_order"] = prev + [s for s in order if s not in prev]
    for s in st["city_order"]:
        cities_mod.ensure_city_bucket(st, s)
    if st.get("city") not in by_slug:
        st["city_index"] = 0
        st["city"] = st["city_order"][0]
    else:
        st["city_index"] = st["city_order"].index(st["city"])
    skip = cities_mod.resolve_skip_cities(
        st, cli=getattr(a, "skip_cities", None),
        env=os.environ.get("AQD_FOODY_SKIP_CITIES"),
        seed=city_rows,
    )
    if skip:
        before = st.get("city")
        cities_mod.skip_current_cities(st, st["city_order"], skip)
        if st.get("city") != before:
            print(f"[skip] bỏ qua {sorted(skip)}; từ {before} -> {st.get('city')} "
                  f"(state city cũ giữ nguyên trong cities[])", flush=True)
        else:
            print(f"[skip] danh sách bỏ qua={sorted(skip)}; đang ở {st.get('city')}", flush=True)
    bucket = cities_mod.ensure_city_bucket(st, st["city"])
    pusher = None
    if not a.no_push:
        from foody_push import Pusher, free_gb
        pusher = Pusher().start()
    page = a.start_page or bucket.get("page", 1)
    c, n = Client(), 0
    if st["city"] not in by_slug:
        raise SystemExit(f"city hiện tại {st.get('city')!r} không có trong danh sách (skip hết?)")
    c.set_city(by_slug[st["city"]])
    print(f"parity={a.parity} city={st['city']} ({st['city_index']+1}/{len(st['city_order'])}) "
          f"page={page} known_total={bucket.get('total')} cities={len(st['city_order'])} "
          f"skip={sorted(skip) or '-'}", flush=True)
    hb("position", _where(page, a.parity, st, bucket))
    hb("start", "foody_fetch.py " + " ".join(sys.argv[1:] if argv is None else argv))
    if pusher:  # reviews / uploads come from the pusher thread's running totals
        hb("track_totals", lambda: {"reviews": pusher.stats["reviews"], "upload_ok": pusher.stats["pushed"],
                                    "upload_fail": pusher.stats["fail"]})
    hb("install_crash_hook")
    hb("start_timer")
    try:
        n = _crawl(a, st, stp, pusher, c, page)
    except KeyboardInterrupt:
        hb("stop", "Ctrl+C"); raise
    hb("stop", "thấy file STOP" if os.path.exists(os.path.join(ROOT, "STOP")) else f"xong {n} quán")
    print(f"xong: {n} quán")


def _where(page, parity, st, bucket=None):
    bucket = bucket if bucket is not None else _active_bucket(st)
    total = bucket.get("total")
    kw = slice_kw(bucket)
    sl = (f" [lát {bucket.get('slice')}/{len(bucket.get('slices') or [])}: quận {kw['district']}"
          + (f", loại {kw['cate']}" if kw["cate"] != "" else "") + "]") if kw else ""
    city = st.get("city") or CITY
    return (f"foody page {page}{sl}, city {city}, parity {parity}"
            + (f" (tổng list {int(total):,})".replace(",", ".") if total else ""))


def _active_bucket(st):
    """Per-city bucket; falls back to st itself for unit tests that still use a flat state."""
    if int(st.get("schema") or 0) >= 2 and isinstance(st.get("cities"), dict):
        import foody_cities as cities_mod
        slug = st.get("city") or (st.get("city_order") or ["ho-chi-minh"])[0]
        return cities_mod.ensure_city_bucket(st, slug)
    return st


def _crawl(a, st, stp, pusher, c, page):
    if pusher:
        from foody_push import free_gb  # imported in main() too, but that name is local there (NameError since #9)
    import foody_cities as cities_mod
    by_slug = {x["slug"]: x for x in (cities_mod.load_cached(ROOT) or cities_mod.seed_cities())}
    # Prefer the live order already on st (filtered in main)
    for slug in st.get("city_order") or []:
        if slug not in by_slug:
            # keep a stub so set_city still works if cache was filtered
            by_slug[slug] = {"slug": slug, "id": CITY_ID, "name": slug, "lat": c.lat, "lng": c.lng}
    n = 0
    while a.limit == 0 or n < a.limit:
        b = _active_bucket(st)
        if os.path.exists(os.path.join(ROOT, "STOP")):
            print("thấy file STOP -> dừng cào" + (", đẩy nốt backlog rồi thoát" if pusher else ""), flush=True)
            if pusher: pusher.drain_and_stop(); pusher.t.join()
            break
        if pusher and free_gb() < a.min_free_gb:
            print(f"ổ còn {free_gb():.1f} GB < {a.min_free_gb} GB -> tạm ngừng cào 5 phút, uploader đang xoá ảnh đã lên", flush=True)
            hb("error", "disk_low", f"Ổ đĩa còn {free_gb():.1f} GB (< {a.min_free_gb:g} GB) → tạm ngừng cào")
            time.sleep(300); continue
        for fid in due_gallery_retries(b):  # #1081: places whose gallery failed earlier
            ent = b["gallery_retry"][fid]
            print(f"  ↻ thử lại ảnh #{fid} (lần {ent['tries'] + 1})", flush=True)
            if process_item(c, ent["item"], a, b, pusher, retry=True) == "ok":
                n += 1
            _save_state(st, stp)
        kw = slice_kw(b)
        try:
            items, total = list_page(c, page, **kw) if kw else list_page(c, page)
        except ConnectTimeoutGiveUp as e:
            page = skip_timeout_page(b, page, e)
            st_city_sync_page(st, b, page)
            _save_state(st, stp); hb("position", _where(page, a.parity, st, b))
            continue
        print(f"[{st.get('city')}] trang {page}{' ' + str(kw) if kw else ''}: {len(items)} quán (tổng {total})", flush=True)
        if not items:
            action, nxt, wait = plan_empty_page(b, page, total)
            if action == "end" and next_slice(b, lambda: _build_slices_live(c)):
                kw = slice_kw(b)
                print(f"hết lát hiện tại (API chỉ trả tối đa {LIST_CAP} quán/lượt) -> sang lát {b['slice']}/"
                      f"{len(b['slices'])} {kw}", flush=True)
                wait = 0
            elif action == "end":
                # City sweep finished -> next city (preserve prior city progress in its own bucket).
                order = st.get("city_order") or [st.get("city") or CITY]
                skip = set(st.get("skip_cities") or [])
                slug, wrapped = cities_mod.advance_city(st, order)
                if slug in skip:
                    slug, wrapped2 = cities_mod.skip_current_cities(st, order, skip)
                    wrapped = wrapped or wrapped2
                if slug and slug in by_slug:
                    c.set_city(by_slug[slug])
                b = _active_bucket(st)
                page = 1; b["page"] = 1; b.pop("slices", None); b["slice"] = 0; b.pop("total", None)
                msg = (f"hết tỉnh/thành, nghỉ {int(END_PAUSE // 60)} phút rồi quét lại từ {slug}"
                       if wrapped else f"xong city trước -> sang {slug}")
                print(msg, flush=True)
                wait = END_PAUSE if wrapped else 2
                nxt = 1
            elif action == "retry":
                print(f"trang {page} trống/không có tổng (đã biết tổng {b.get('total')}) -> thử lại sau {int(wait)}s "
                      f"(lần {b['list_retry']}/{LIST_RETRIES}), KHÔNG quay về trang 1", flush=True)
            else:
                hb("empty_page", str(page))
                print(f"trang {page} vẫn trống sau {LIST_RETRIES} lần -> bỏ qua, sang trang {nxt}"
                      + (f"; nhiều trang liền bị bỏ, nghỉ {int(wait // 60)} phút" if wait else ""), flush=True)
            page = nxt; b["page"] = page; _save_state(st, stp); hb("position", _where(page, a.parity, st, b))
            if wait: time.sleep(wait)
            continue
        note_page_ok(b, total); hb("page_ok")
        for it in items:
            if a.limit and n >= a.limit:
                break
            fid = str(it["Id"])
            if a.parity != "all" and (int(fid) % 2 == 1) != (a.parity == "odd"):
                continue
            if fid in b["done"] and not a.refetch:
                continue
            if fid in (b.get("gallery_retry") or {}):
                continue  # waits for its own backoff slot
            if process_item(c, it, a, b, pusher) in ("ok", "none"):
                n += 1
            _save_state(st, stp)
        page += 1; b["page"] = page
        _save_state(st, stp); hb("position", _where(page, a.parity, st, b))
    return n


def st_city_sync_page(st, b, page):
    b["page"] = page


def skip_timeout_page(st, page, exc):
    """A list page that only ever times out: log it, tell Slack, move on to the next page (never crash)."""
    nxt = page + 1
    msg = f"trang {page} ConnectTimeout {exc.count} lần liên tiếp -> bỏ qua, sang trang {nxt}"
    print(msg, flush=True)
    skipped = st.setdefault("timeout_skipped", [])
    skipped.append({"page": page, "slice": st.get("slice"), "at": now()})
    del skipped[:-50]
    hb("error", "foody_list_timeout", msg, action="kiểm tra mạng / foody.vn trên máy này; trang đã bỏ qua", severe=True)
    st["page"] = nxt
    return nxt


def _build_slices_live(c):
    def total_of(d, k):
        try:
            return list_page(c, 1, district=d, cate=k)[1]
        except Exception:
            return None
    districts = fetch_district_ids(c)
    if districts:
        print(f"dựng danh sách lát từ {len(districts)} quận/huyện Foody "
              f"(provinceId={getattr(c, 'city_id', '?')}, mỗi lát <= {LIST_CAP} quán)...", flush=True)
        out = build_slices(total_of, district_ids=districts)
    else:
        print(f"dựng danh sách lát (fallback probe 1..{SLICE_MAX_ID}, mỗi lát <= {LIST_CAP} quán)...", flush=True)
        out = build_slices(total_of)
    print(f"  {len(out)} lát, tổng {sum(x['total'] for x in out)} quán", flush=True)
    if not out:
        print("  ! 0 lát — city này sẽ được bỏ qua (không có district/cate trả Total>0)", flush=True)
    return out


def _save_state(st, stp):
    # Mirror the active city bucket to top-level keys so older tools/tests that read page/done still work.
    b = _active_bucket(st)
    for k in ("page", "done", "failed", "total", "slices", "slice", "list_retry", "skip_streak",
              "skipped_pages", "gallery_retry", "gallery_failed", "timeout_skipped"):
        if k in b:
            st[k] = b[k]
    if os.path.exists(stp):
        try: os.replace(stp, stp + ".bak")
        except OSError: pass
    tmp = stp + ".tmp"; json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False); os.replace(tmp, stp)


if __name__ == "__main__":
    main()
