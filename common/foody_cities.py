"""Foody city / province list for multi-city crawling.

Seed: data/foody_cities.json (Foody province id + URL slug + display name + center lat/lng).
Discovery: GET /{slug}/o-dau and require cookie floc == id; drop dead slugs. Cache verified list in
foody_data/_cities.json so a cold start does not re-probe every run.
Exclude "toan-quoc" (nationwide pseudo-city).
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE.parent / "data" / "foody_cities.json"
SKIP_SLUGS = frozenset({"toan-quoc"})


def seed_cities():
    rows = json.loads(SEED.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        slug = (r.get("slug") or "").strip()
        if not slug or slug in SKIP_SLUGS:
            continue
        out.append({
            "id": int(r["id"]),
            "slug": slug,
            "name": r.get("name") or slug,
            "lat": float(r.get("lat") or 0) or None,
            "lng": float(r.get("lng") or 0) or None,
        })
    return out


def load_cached(root):
    p = Path(root) / "_cities.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = d.get("cities") if isinstance(d, dict) else d
    if not isinstance(rows, list) or not rows:
        return None
    return rows


def save_cached(root, cities, source="verify"):
    Path(root).mkdir(parents=True, exist_ok=True)
    p = Path(root) / "_cities.json"
    tmp = str(p) + ".tmp"
    payload = {"source": source, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "count": len(cities), "cities": cities}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def verify_city(session, city, base="https://www.foody.vn", timeout=20):
    """True when /{slug}/o-dau sets floc to the expected province id."""
    slug, cid = city["slug"], int(city["id"])
    # fresh cookies for this probe
    session.cookies.clear()
    r = session.get(f"{base}/{slug}/o-dau", timeout=timeout, allow_redirects=True)
    if r.status_code != 200 or "/o-dau" not in r.url:
        return False
    if "không tìm thấy" in (r.text or "")[:2500].lower():
        return False
    floc = session.cookies.get("floc")
    return str(floc) == str(cid)


def discover_cities(session=None, root=None, verify=True, seed=None):
    """Return the city list to crawl. verify=True probes Foody (needs network); False uses seed/cache as-is."""
    seed = seed if seed is not None else seed_cities()
    if root:
        cached = load_cached(root)
        if cached and not verify:
            return cached
    if not verify:
        return seed
    import requests
    s = session or requests.Session()
    if not getattr(s, "headers", None) or "User-Agent" not in s.headers:
        s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; script_foody/cities)"})
    ok = []
    for city in seed:
        try:
            if verify_city(s, city):
                ok.append(city)
                print(f"  [city ✓] {city['slug']} ({city['id']}) {city.get('name')}", flush=True)
            else:
                print(f"  [city ✗] {city['slug']} ({city['id']}) — bỏ", flush=True)
        except Exception as e:
            print(f"  [city ✗] {city['slug']}: {type(e).__name__}: {e}", flush=True)
        time.sleep(0.15)
    if root and ok:
        save_cached(root, ok, source="verify")
    return ok or seed


def filter_cities(cities, wanted):
    """wanted: None/'all'/'' -> all; comma slugs or ids; single slug."""
    if not wanted or str(wanted).strip().lower() in ("all", "*"):
        return list(cities)
    parts = [p.strip() for p in str(wanted).split(",") if p.strip()]
    slugs = {p.lower() for p in parts if not p.isdigit()}
    ids = {int(p) for p in parts if p.isdigit()}
    out = [c for c in cities if c["slug"].lower() in slugs or int(c["id"]) in ids]
    return out or list(cities)


def migrate_state(st, default_slug="ho-chi-minh"):
    """Lift a legacy flat _state.json into schema=2 with per-city buckets. Idempotent."""
    if not isinstance(st, dict):
        st = {}
    if int(st.get("schema") or 0) >= 2 and isinstance(st.get("cities"), dict):
        st.setdefault("city_order", list(st["cities"].keys()) or [default_slug])
        st.setdefault("city_index", 0)
        return st
    # Legacy fields that belong inside one city bucket
    keys = ("page", "done", "failed", "total", "slices", "slice", "list_retry", "skip_streak",
            "skipped_pages", "gallery_retry", "gallery_failed", "timeout_skipped", "list_timeout")
    bucket = {}
    for k in keys:
        if k in st:
            bucket[k] = st.pop(k)
    bucket.setdefault("page", 1)
    bucket.setdefault("done", {})
    bucket.setdefault("failed", {})
    slug = st.get("city") or default_slug
    st["schema"] = 2
    st["cities"] = {slug: bucket}
    st["city_order"] = [slug]
    st["city_index"] = 0
    st["city"] = slug
    return st


def ensure_city_bucket(st, slug):
    cities = st.setdefault("cities", {})
    if slug not in cities:
        cities[slug] = {"page": 1, "done": {}, "failed": {}}
    b = cities[slug]
    b.setdefault("done", {}); b.setdefault("failed", {}); b.setdefault("page", 1)
    return b


def current_city(st, city_order):
    order = st.get("city_order") or city_order
    if not order:
        return None
    i = int(st.get("city_index") or 0)
    if i < 0 or i >= len(order):
        i = 0
        st["city_index"] = 0
    return order[i]


def advance_city(st, city_order):
    """Move to the next city. Returns (slug_or_None, wrapped): wrapped=True when we looped to the first city."""
    order = st.get("city_order") or city_order
    if not order:
        return None, True
    i = int(st.get("city_index") or 0) + 1
    wrapped = i >= len(order)
    if wrapped:
        i = 0
    st["city_index"] = i
    slug = order[i]
    st["city"] = slug
    ensure_city_bucket(st, slug)
    return slug, wrapped
