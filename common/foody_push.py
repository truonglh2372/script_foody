"""Đẩy từng quán Foody lên server ngay sau khi cào + xoá ảnh local khi server đã lưu xong.

Dùng chung logic payload/photos_of/reviews_of của foody_upload.py (v1 import endpoints, source=foody).
- Credentials: chỉ từ env AQD_ADMIN_EMAIL / AQD_ADMIN_PASSWORD; nếu chưa có thì nạp <AQD_ROOT>/creds.env
  (AQD_ROOT mặc định C:/aqd trên Windows, ~/aqd trên Mac). Không bao giờ in secret.
- Tiến độ: foody_data/_uploaded.json (quán -> restaurant_id, photos đã gửi), foody_data/_cleaned.json (quán đã xoá ảnh).
- Chỉ xoá file trong <fid>/photos/ khi MỌI ảnh của quán đã gửi VÀ server liệt kê từng ảnh status=succeeded
  (hoặc duplicate_of_id). Giữ place.json, _state.json, _uploaded.json, _reviews_pending.jsonl.
- Lỗi upload: giữ file, lần lặp/khởi động sau thử lại.
"""
import argparse, json, os, sys, threading, time, traceback, queue, shutil

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
if not os.environ.get("AQD_ROOT"):
    os.environ["AQD_ROOT"] = "C:/aqd" if os.name == "nt" else os.path.expanduser("~/aqd")


def load_creds():
    if os.environ.get("AQD_ADMIN_EMAIL") and os.environ.get("AQD_ADMIN_PASSWORD"):
        return
    p = os.path.join(os.environ["AQD_ROOT"], "creds.env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


import foody_upload as fu  # noqa: E402
from aqd_api import APIError  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

SERVER_COORD_CODES = ("import_coordinates_out_of_range", "import_coordinates_required")  # server 422s (#1452)

ROOT = fu.ROOT; EP = fu.EP
UP = os.path.join(ROOT, "_uploaded.json"); CL = os.path.join(ROOT, "_cleaned.json")
BAD = os.path.join(ROOT, "_bad_coords.json")  # fid -> {problems, lat, lng, name, url}: not sent, needs a corrected point
RP = os.path.join(ROOT, "_reviews_pushed.json")  # fid -> [review source_id đã lên server]
REV = os.path.join(ROOT, "_reviews_pending.jsonl")
_lock = threading.Lock()


def _read(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def _write(p, obj):
    tmp = p + ".tmp"
    json.dump(obj, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def load_place(fid):
    try:
        return json.load(open(os.path.join(ROOT, fid, "place.json"), encoding="utf-8"))
    except Exception as e:
        print(f"  [skip-json] #{fid}: {e}", flush=True)
        return None


def free_gb(path=ROOT):
    return shutil.disk_usage(path).free / 1e9


class Pusher:
    def __init__(self):
        load_creds()
        from aqd_api import AQD
        self.api = AQD()
        self.done = _read(UP, {}); self.cleaned = _read(CL, {}); self.rpushed = _read(RP, {})
        self.bad = _read(BAD, {})
        self.q = queue.Queue(); self.stats = {"reviews": 0, "pushed": 0, "photos": 0, "deleted_files": 0, "freed_bytes": 0, "fail": 0}

    def _save(self):
        with _lock:
            _write(UP, self.done); _write(CL, self.cleaned); _write(RP, self.rpushed); _write(BAD, self.bad)

    def append_reviews(self, fid, p):
        seen = set()
        if os.path.exists(REV):
            for line in open(REV, encoding="utf-8"):
                try: seen.add(json.loads(line).get("source_id"))
                except Exception: pass
        with open(REV, "a", encoding="utf-8") as f:
            for r in fu.reviews_of(p):
                if r["source_id"] not in seen:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def flag_bad_coords(self, fid, p, problems):
        """Record (once per distinct problem) a place whose point is missing or invalid; it is never sent as 0 / garbage."""
        entry = {"problems": problems, "lat": p.get("lat"), "lng": p.get("lng"), "name": p.get("name"),
                 "url": p.get("source_url") or p.get("url"), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if (self.bad.get(fid) or {}).get("problems") != problems:
            print(f"  [coords ✗] #{fid} {p.get('name')}: {','.join(problems)} (lat={p.get('lat')}, lng={p.get('lng')}) -> không gửi, "
                  f"ghi _bad_coords.json", flush=True)
            self.bad[fid] = entry; self._save()

    def push(self, fid):
        """Import 1 quán + ảnh còn thiếu. True nếu mọi ảnh đã gửi."""
        p = load_place(fid)
        if p is None:
            return False
        body, ph = fu.payload(p), fu.photos_of(p)
        bad = fu.problems(body)
        if bad:
            self.flag_bad_coords(fid, p, bad)
            return False
        if self.bad.pop(fid, None):  # a re-fetch brought a valid point
            self._save()
        rec = self.done.get(fid) or {}
        if not rec.get("restaurant_id"):
            try:
                r = self.api.call("POST", EP, json_body=body)
            except APIError as e:  # an_uong_quanh_day#1452: server guard, a retry can never succeed
                if e.status == 422 and e.code in SERVER_COORD_CODES:
                    self.flag_bad_coords(fid, p, ["server_" + e.code])
                    return False
                raise
            if not isinstance(r, dict) or not r.get("restaurant_id"):
                raise RuntimeError(f"import trả về không có restaurant_id: {str(r)[:200]}")
            cands = (r.get("duplicate") or {}).get("candidates") or []
            rec = {"restaurant_id": r["restaurant_id"], "created": r.get("created"),
                   "duplicate_suspected": bool((r.get("duplicate") or {}).get("suspected")),
                   "duplicate_of": [{"restaurant_id": c.get("restaurant_id"), "score": c.get("score"),
                                     "reasons": c.get("reasons")} for c in cands], "photos": []}
            self.done[fid] = rec; self._save(); self.stats["pushed"] += 1
        rid = rec["restaurant_id"]; sent = set(rec.get("photos") or []); missing_file = 0
        for x in ph:
            if x["source_id"] in sent:
                continue
            path = os.path.join(ROOT, fid, x["file"])
            if not os.path.exists(path):
                missing_file += 1; continue
            with open(path, "rb") as f:
                self.api.call("POST", f"{EP}/{rid}/photos",
                              data={"source": "foody", "source_id": x["source_id"], "source_url": x.get("source_url") or "",
                                    "role": x["_role"]},
                              files={"image": (os.path.basename(path), f, "image/jpeg")}, timeout=90)
            rec.setdefault("photos", []).append(x["source_id"]); self.stats["photos"] += 1; self._save()
        return missing_file == 0

    @staticmethod
    def has_photo_files(fid):
        pd = os.path.join(ROOT, fid, "photos")
        return os.path.isdir(pd) and any(os.path.isfile(os.path.join(pd, f)) for f in os.listdir(pd))

    def cleanup(self, fid):
        """Xoá ảnh local khi server đã lưu đủ. True nếu đã xoá (hoặc không còn gì để xoá).
        Quán đã 'cleaned' mà lại có file ảnh (bị cào lại) thì kiểm lại với server rồi mới xoá."""
        if fid in self.cleaned and not self.has_photo_files(fid):
            return True
        rec = self.done.get(fid); p = load_place(fid)
        if not rec or p is None:
            return False
        want = {x["source_id"] for x in fu.photos_of(p)}
        if not want <= set(rec.get("photos") or []):
            return False
        ok, pg = set(), 1
        while want:
            r = self.api.call("GET", f"/v1/admin/restaurants/{rec['restaurant_id']}/photos?per_page=100&page={pg}")
            r = r if isinstance(r, dict) else {}
            for x in r.get("photos") or []:
                if not isinstance(x, dict):
                    continue  # id/chuỗi thay vì object: không xác nhận được -> coi như chưa xong
                if x.get("import_source") == "foody" and (x.get("status") == "succeeded" or x.get("duplicate_of_id")):
                    ok.add(str(x.get("source_id")))
            if not r.get("has_more"):
                break
            pg += 1
        if want - ok:
            return False  # còn pending/thiếu -> giữ file, lần sau kiểm lại
        pd = os.path.join(ROOT, fid, "photos")
        if os.path.isdir(pd):
            for f in os.listdir(pd):
                fp = os.path.join(pd, f)
                if os.path.isfile(fp):
                    self.stats["freed_bytes"] += os.path.getsize(fp); os.remove(fp); self.stats["deleted_files"] += 1
        self.cleaned[fid] = rec["restaurant_id"]; self._save()
        return True

    @staticmethod
    def review_rows(p):
        """Review Foody -> body cho POST /v1/admin/imports/restaurants/:id/reviews (#736). rating_original thang 0-10."""
        rows = []
        for r in fu.reviews_of(p):
            try:
                ro = float(r.get("rating"))
            except (TypeError, ValueError):
                continue
            if not 0 <= ro <= 10:
                continue
            # #7 mục 4: published_at (server: source_created_at, ISO 8601). Tiêu đề: server CHƯA có field -> không gửi.
            row = {"source": "foody", "source_id": r["source_id"], "source_url": r.get("source_url") or None,
                   "author_name": r.get("author_name"), "rating_original": ro, "text": r.get("text") or "",
                   "published_at": fu.iso_time(r.get("created_at"))}
            rows.append({k: v for k, v in row.items() if v is not None})
        return rows[:fu.MAX_REVIEWS]

    def push_reviews(self, fid, p=None):
        """Đẩy review của quán đã import; idempotent (server upsert theo source+source_id, local _reviews_pushed.json)."""
        rec = self.done.get(fid) or {}
        if not rec.get("restaurant_id"):
            return False
        p = p or load_place(fid)
        if p is None:
            return False
        sent = set(self.rpushed.get(fid) or [])
        todo = [r for r in self.review_rows(p) if r["source_id"] not in sent]
        for i in range(0, len(todo), 20):
            chunk = todo[i:i + 20]
            self.api.call("POST", f"{EP}/{rec['restaurant_id']}/reviews", json_body={"reviews": chunk}, timeout=90)
            self.rpushed.setdefault(fid, []).extend(r["source_id"] for r in chunk)
            self.stats["reviews"] += len(chunk); self._save()
        self.rpushed.setdefault(fid, [])
        return True

    def reviews_pending(self, fid):
        return fid not in self.rpushed

    def handle(self, fid):
        try:
            if self.push(fid):
                self.cleanup(fid)
            if self.reviews_pending(fid):
                self.push_reviews(fid)
            return True
        except Exception as e:
            self.stats["fail"] += 1
            print(f"  [push ✗] #{fid}: {type(e).__name__} {getattr(e, 'status', '')} {str(e)[:300]}\n"
                  + "".join("    " + ln for ln in traceback.format_exc().splitlines(True)), flush=True)
            return False

    def backlog(self):
        ids = sorted(d for d in os.listdir(ROOT) if d not in self.bad and (d not in self.cleaned or (d in self.done and self.reviews_pending(d))
                                                     or self.has_photo_files(d))
                     and os.path.exists(os.path.join(ROOT, d, "place.json")))
        return ids

    # ---- worker thread: hàng đợi quán mới trước, rảnh thì quét backlog ----
    def start(self, sweep_every=600):
        def run():
            last = 0
            while True:
                try:
                    fid = self.q.get(timeout=5)
                except queue.Empty:
                    fid = None
                if fid is None:
                    if time.time() - last < sweep_every:
                        continue
                    last = time.time(); n = 0
                    for b in self.backlog():
                        if not self.q.empty():
                            break
                        self.handle(b); n += 1
                    if n:
                        print(f"  [push] backlog quét {n} quán | {json.dumps(self.stats)} | trống {free_gb():.1f} GB", flush=True)
                    continue
                if fid == "__STOP__":
                    break
                if self.handle(fid) and self.done.get(fid):
                    rec = self.done.get(fid) or {}
                    print(f"  [push ✓] #{fid} -> quán {rec.get('restaurant_id')} | {len(rec.get('photos') or [])} ảnh"
                          f"{' | đã xoá ảnh local' if fid in self.cleaned else ' | giữ ảnh (chờ server)'}", flush=True)
        self.t = threading.Thread(target=run, daemon=True); self.t.start()
        return self

    def drain_and_stop(self):
        """Dùng khi STOP: đẩy hết backlog rồi dừng."""
        for b in self.backlog():
            self.q.put(b)
        self.q.put("__STOP__")


# ---- #7 mục 7: đẩy bù cho quán ĐÃ tạo (có restaurant_id trong _uploaded.json), không tạo lại quán ----
def backfill_ids(done, ids=None):
    return [f for f in (ids or sorted(done)) if (done.get(f) or {}).get("restaurant_id")]


def backfill(pusher, ids=None, apply=False, out=print):
    """Với mỗi quán đã có restaurant_id:
    1. gửi lại payload import (server tìm theo source+source_id -> created=false, chỉ ĐIỀN field đang trống như phone;
       không tạo quán mới, không đổi menu/giờ đã có). restaurant_id trả về khác bản ghi -> dừng quán đó, không ghi đè.
    2. gửi review còn thiếu (chưa có trong _reviews_pushed.json), tối đa AQD_MAX_REVIEWS.
    3. dọn ảnh local nếu server đã lưu đủ (cleanup như thường).
    Mặc định dry-run: chỉ in việc sẽ làm, không gọi server."""
    st = {"places": 0, "refilled": 0, "reviews": 0, "fail": 0, "mismatch": 0}
    for fid in backfill_ids(pusher.done, ids):
        rec = pusher.done[fid]; rid = rec["restaurant_id"]; p = load_place(fid)
        if p is None:
            continue
        st["places"] += 1
        body = fu.payload(p)
        sent = set(pusher.rpushed.get(fid) or [])
        todo = [r for r in pusher.review_rows(p) if r["source_id"] not in sent]
        if not apply:
            out(f"[dry] #{fid} -> quán {rid} | phones {body.get('phones')} | {len(todo)} review sẽ gửi")
            st["reviews"] += len(todo); continue
        try:
            if not fu.problems(body):
                r = pusher.api.call("POST", EP, json_body=body)
                got = r.get("restaurant_id") if isinstance(r, dict) else None
                if str(got) != str(rid):
                    st["mismatch"] += 1
                    out(f"[backfill ✗] #{fid}: server trả quán {got}, bản ghi là {rid} -> bỏ qua, cần admin xem"); continue
                st["refilled"] += 1
            n0 = pusher.stats["reviews"]
            pusher.push_reviews(fid, p)
            st["reviews"] += pusher.stats["reviews"] - n0
            pusher.cleanup(fid)
            out(f"[backfill ✓] #{fid} -> quán {rid} | +{pusher.stats['reviews'] - n0} review")
        except Exception as e:
            st["fail"] += 1
            out(f"[backfill ✗] #{fid}: {type(e).__name__} {str(e)[:300]}\n" + traceback.format_exc())
    return st


def main(argv=None):
    ap = argparse.ArgumentParser(description="Đẩy bù review/field cho quán Foody đã tạo (không tạo lại quán)")
    ap.add_argument("--backfill", action="store_true", required=True)
    ap.add_argument("--ids", nargs="*", help="Foody id; mặc định mọi quán có restaurant_id trong _uploaded.json")
    ap.add_argument("--apply", action="store_true", help="gửi thật (mặc định dry-run)")
    a = ap.parse_args(argv)
    if a.apply:
        pusher = Pusher()
    else:
        pusher = Pusher.__new__(Pusher)
        pusher.done = _read(UP, {}); pusher.cleaned = _read(CL, {}); pusher.rpushed = _read(RP, {})
        pusher.stats = {"reviews": 0}
    st = backfill(pusher, a.ids, apply=a.apply)
    print("TỔNG", json.dumps(st, ensure_ascii=False))
    if not a.apply:
        print("DRY-RUN: không gửi gì lên server. Thêm --apply để gửi thật.")


if __name__ == "__main__":
    main()
