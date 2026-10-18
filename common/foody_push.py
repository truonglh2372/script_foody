"""Đẩy từng quán Foody lên server ngay sau khi cào + xoá ảnh local khi server đã lưu xong.

Dùng chung logic payload/photos_of/reviews_of của foody_upload.py (v1 import endpoints, source=foody).
- Credentials: chỉ từ env AQD_ADMIN_EMAIL / AQD_ADMIN_PASSWORD; nếu chưa có thì nạp <AQD_ROOT>/creds.env
  (AQD_ROOT mặc định C:/aqd trên Windows, ~/aqd trên Mac). Không bao giờ in secret.
- Tiến độ: foody_data/_uploaded.json (quán -> restaurant_id, photos đã gửi), foody_data/_cleaned.json (quán đã xoá ảnh).
- Chỉ xoá file trong <fid>/photos/ khi MỌI ảnh của quán đã gửi VÀ server liệt kê từng ảnh status=succeeded
  (hoặc duplicate_of_id). Giữ place.json, _state.json, _uploaded.json, _reviews_pending.jsonl.
- Lỗi upload: giữ file, lần lặp/khởi động sau thử lại.
"""
import json, os, sys, threading, time, queue, shutil

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

ROOT = fu.ROOT; EP = fu.EP
UP = os.path.join(ROOT, "_uploaded.json"); CL = os.path.join(ROOT, "_cleaned.json")
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
        self.done = _read(UP, {}); self.cleaned = _read(CL, {})
        self.q = queue.Queue(); self.stats = {"pushed": 0, "photos": 0, "deleted_files": 0, "freed_bytes": 0, "fail": 0}

    def _save(self):
        with _lock:
            _write(UP, self.done); _write(CL, self.cleaned)

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

    def push(self, fid):
        """Import 1 quán + ảnh còn thiếu. True nếu mọi ảnh đã gửi."""
        p = load_place(fid)
        if p is None:
            return False
        body, ph = fu.payload(p), fu.photos_of(p)
        if fu.problems(body):
            return False
        rec = self.done.get(fid) or {}
        if not rec.get("restaurant_id"):
            r = self.api.call("POST", EP, json_body=body)
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

    def cleanup(self, fid):
        """Xoá ảnh local khi server đã lưu đủ. True nếu đã xoá (hoặc không còn gì để xoá)."""
        if fid in self.cleaned:
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
            for x in r.get("photos") or []:
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

    def handle(self, fid):
        try:
            if self.push(fid):
                self.cleanup(fid)
            return True
        except Exception as e:
            self.stats["fail"] += 1
            print(f"  [push ✗] #{fid}: {type(e).__name__} {getattr(e, 'status', '')} {str(e)[:200]}", flush=True)
            return False

    def backlog(self):
        ids = sorted(d for d in os.listdir(ROOT) if d not in self.cleaned and os.path.exists(os.path.join(ROOT, d, "place.json")))
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
                if self.handle(fid):
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
