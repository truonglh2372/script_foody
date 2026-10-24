"""Tests for the Foody list/gallery/photo/review fixes in common/foody_fetch.py.

  #1080  an empty list page (or one without Total) below the known total is retried with backoff, then skipped;
            the crawler never jumps back to page 1 early, and the page reached is persisted.
  #1081  a gallery fetch that errors (503/busy give-up, non-JSON) or comes back empty although the album counts
            say there are photos leaves the place NOT done and queued for a retry.
  #1083  reviews: up to AQD_MAX_REVIEWS (default 50) per place, paging the review list until the cap or the end.
  #1082  photos: ONLY menu then dish photos, AQD_FOODY_MAX_PHOTOS (default 5) in total.

A fake client stands in for foody.vn; no network, no server.
Run: python3 tests/test_list_and_gallery.py
"""
import importlib, json, os, re, sys, tempfile, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
os.environ.setdefault("AQD_FOODY_DATA", tempfile.mkdtemp(prefix="foody_test_"))
import foody_fetch as ff  # noqa: E402


def gallery_rows(album, n, start=0):
    return [{"Id": album * 1000 + start + i, "FullSizeImageUrl": f"https://img/{album}/{start + i}.jpg", "MediaType": 1,
             "Size": {"Width": 800, "Height": 600}} for i in range(n)]


class FakeClient:
    """routes: predefineAlbumId -> list of photo rows or an Exception / None (non-JSON); review pages by LastId."""

    def __init__(self, albums=None, reviews=None, review_error_at=None):
        self.albums = albums or {}
        self.reviews = reviews or []
        self.review_error_at = review_error_at
        self.calls = []

    def json(self, path, referer=None):
        self.calls.append(path)
        if "ListRestaurantPicturesWithPaging" in path:
            album = int(re.search(r"predefineAlbumId=(\d+)", path).group(1))
            page = int(re.search(r"pageIndex=(\d+)", path).group(1))
            size = int(re.search(r"pageSize=(\d+)", path).group(1))
            v = self.albums.get(album, [])
            if isinstance(v, Exception):
                raise v
            if v is None:
                return None
            return {"data": v[page * size: (page + 1) * size], "total": len(v)}  # foody.vn: pageIndex is 0-based
        if "ResLoadMore" in path:
            last = re.search(r"LastId=(\d*)", path).group(1)
            n = sum(1 for p in self.calls if "ResLoadMore" in p)
            if self.review_error_at and n >= self.review_error_at:
                raise RuntimeError("bỏ cuộc sau 6 lần")
            i = 0 if not last else next(k for k, r in enumerate(self.reviews) if str(r["Id"]) == last) + 1
            page = self.reviews[i:i + 10]
            return {"Items": page, "LastId": page[-1]["Id"] if page else int(last or 0)}
        raise AssertionError(path)


def review_items(n):
    return [{"Id": 9000 - i, "Title": f"t{i}", "Description": f"review {i}", "AvgRating": 7.5,
             "CreatedDate": "/Date(1651828871213)/", "Url": f"/ho-chi-minh/x/binh-luan-{9000 - i}",
             "Owner": {"DisplayName": f"user {i}"}} for i in range(n)]


class ListPaging(unittest.TestCase):  # #1080
    def test_missing_total_on_first_ever_page_is_end(self):
        st = {}
        self.assertEqual(ff.plan_empty_page(st, 1, None)[0], "end")

    def test_missing_total_below_known_total_retries_same_page_with_backoff(self):
        st = {"total": 12 * 500}
        waits = []
        for _ in range(3):
            action, nxt, wait = ff.plan_empty_page(st, 120, None, retries=5, backoff=30, backoff_max=600)
            self.assertEqual((action, nxt), ("retry", 120))
            waits.append(wait)
        self.assertEqual(waits, [30, 60, 120])

    def test_retries_exhausted_skips_forward_never_page_one(self):
        st = {"total": 12 * 500}
        for _ in range(5):
            ff.plan_empty_page(st, 120, None, retries=5, backoff=1, backoff_max=1)
        action, nxt, wait = ff.plan_empty_page(st, 120, None, retries=5, backoff=1, backoff_max=1, skip_streak=5)
        self.assertEqual((action, nxt, wait), ("skip", 121, 0))
        self.assertEqual(st["skipped_pages"], [120])

    def test_skip_streak_triggers_long_pause(self):
        st = {"total": 12 * 500}
        waits = []
        for page in range(100, 103):
            ff.plan_empty_page(st, page, None, retries=1, backoff=1, backoff_max=1, skip_streak=3)  # retry
            action, nxt, wait = ff.plan_empty_page(st, page, None, retries=1, backoff=1, backoff_max=1, skip_streak=3)
            self.assertEqual((action, nxt), ("skip", page + 1))
            waits.append(wait)
        self.assertEqual(waits, [0, 0, ff.LIST_SKIP_PAUSE])
        self.assertEqual(st["skip_streak"], 0)
        self.assertEqual(st["skipped_pages"], [100, 101, 102])

    def test_past_known_total_is_end(self):
        st = {"total": 120}
        self.assertEqual(ff.plan_empty_page(st, 11, None)[0], "end")   # (11-1)*12 = 120 >= 120
        self.assertEqual(ff.plan_empty_page(st, 10, None)[0], "retry")

    def test_page_ok_resets_counters_and_remembers_total(self):
        st = {"list_retry": 3, "skip_streak": 2}
        ff.note_page_ok(st, 6000)
        self.assertEqual((st["list_retry"], st["skip_streak"], st["total"]), (0, 0, 6000))

    def test_main_persists_page_and_does_not_restart(self):
        """main() with an empty page below the known total: state keeps page 120 (retry), not page 1."""
        root = tempfile.mkdtemp(prefix="foody_main_")
        stp = os.path.join(root, "_state.json")
        json.dump({"page": 120, "done": {}, "failed": {}, "total": 6000}, open(stp, "w"))
        calls = []

        def fake_list(c, page):
            calls.append(page)
            if len(calls) >= 2:
                open(os.path.join(root, "STOP"), "w").close()
            return [], None

        with mock.patch.object(ff, "ROOT", root), mock.patch.object(ff, "list_page", fake_list), \
                mock.patch.object(ff.time, "sleep", lambda s: None), mock.patch.object(ff, "_make_heartbeat", lambda: None):
            ff.main(["--limit", "0", "--no-push"])
        self.assertEqual(calls, [120, 120])
        with open(stp) as f:
            self.assertEqual(json.load(f)["page"], 120)


class Gallery(unittest.TestCase):  # #1081 + #1082
    def test_menu_first_then_dishes_max_five(self):
        c = FakeClient({3: gallery_rows(3, 2), 2: gallery_rows(2, 30), 1: gallery_rows(1, 9), 4: gallery_rows(4, 9)})
        out = ff.photos(c, 1, "/x", 5, expected={"3": 2, "2": 30, "1": 9, "4": 9})
        self.assertEqual([p["album"] for p in out], ["menu", "menu", "food", "food", "food"])
        self.assertTrue(all(p["keep"] for p in out))
        self.assertFalse(any("predefineAlbumId=1" in x or "predefineAlbumId=4" in x for x in c.calls))  # never fetched

    def test_first_gallery_request_is_page_zero(self):  # #1081 root cause: foody.vn pageIndex is 0-based
        c = FakeClient({2: gallery_rows(2, 7)})
        out = ff.photos(c, 1000000511, "/x", 5, expected={"2": 7})
        self.assertEqual(len(out), 5)
        first = [x for x in c.calls if "ListRestaurantPicturesWithPaging" in x][0]
        self.assertIn("pageIndex=0&", first)
        self.assertFalse(any("pageIndex=1&" in x for x in c.calls))  # fewer than 20 rows: one page only

    def test_second_page_is_index_one(self):
        c = FakeClient({3: gallery_rows(3, 25)})
        out = ff.photos(c, 1, "/x", 25, expected={"3": 25})
        self.assertEqual(len([p for p in out if p["album"] == "menu"]), min(25, len(out)))
        pages = [int(__import__("re").search(r"pageIndex=(\d+)", x).group(1)) for x in c.calls if "predefineAlbumId=3" in x]
        self.assertEqual(pages[:2], [0, 1])

    def test_menu_alone_fills_the_cap(self):
        c = FakeClient({3: gallery_rows(3, 40), 2: gallery_rows(2, 5)})
        out = ff.photos(c, 1, "/x", 5, expected={3: 40, 2: 5})
        self.assertEqual([p["album"] for p in out], ["menu"] * 5)
        self.assertFalse(any("predefineAlbumId=2" in x for x in c.calls))

    def test_default_cap_is_five(self):
        self.assertEqual(ff.MAX_PHOTOS, int(os.environ.get("AQD_FOODY_MAX_PHOTOS", "5")))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AQD_FOODY_MAX_PHOTOS", None)
            fresh = importlib.reload(ff)
            self.assertEqual(fresh.MAX_PHOTOS, 5)
            self.assertEqual(fresh.main.__code__.co_name, "main")

    def test_env_overrides_cap(self):
        with mock.patch.dict(os.environ, {"AQD_FOODY_MAX_PHOTOS": "3"}):
            fresh = importlib.reload(ff)
            self.assertEqual(fresh.MAX_PHOTOS, 3)
        importlib.reload(ff)

    def test_filter_drops_venue_people_other_and_orders(self):
        items = [{"album": "interior"}, {"album": "food", "n": 1}, {"album": "other"}, {"album": "menu", "n": 2},
                 {"album": "food", "n": 3}, {"album": "people"}]
        kept = ff.filter_photos(items, 5)
        self.assertEqual([(x["album"], x.get("n")) for x in kept], [("menu", 2), ("food", 1), ("food", 3)])
        self.assertEqual(len(ff.filter_photos([{"album": "food"}] * 9, 5)), 5)

    def test_album_counts_zero_skip_request(self):
        c = FakeClient({3: [], 2: gallery_rows(2, 3)})
        out = ff.photos(c, 1, "/x", 5, expected={3: 0, 2: 3})
        self.assertEqual(len(out), 3)
        self.assertFalse(any("predefineAlbumId=3" in x for x in c.calls))

    def test_busy_error_raises_gallery_error(self):
        c = FakeClient({3: RuntimeError("bỏ cuộc sau 6 lần"), 2: gallery_rows(2, 3)})
        with self.assertRaises(ff.GalleryError):
            ff.photos(c, 1, "/x", 5, expected={3: 4, 2: 3})

    def test_non_json_raises_gallery_error(self):
        c = FakeClient({3: [], 2: None})
        with self.assertRaises(ff.GalleryError):
            ff.photos(c, 1, "/x", 5, expected={3: 0, 2: 7})

    def test_empty_while_counts_say_photos_raises(self):
        c = FakeClient({3: [], 2: []})
        with self.assertRaises(ff.GalleryError):
            ff.photos(c, 1, "/x", 5, expected={3: 2, 2: 7})

    def test_only_venue_photos_is_not_an_error(self):
        c = FakeClient({1: gallery_rows(1, 9)})
        self.assertEqual(ff.photos(c, 1, "/x", 5, expected={1: 9, 4: 3}), [])

    def test_gallery_error_leaves_place_not_done_and_queued(self):
        st = {"done": {}, "failed": {}}
        a = types.SimpleNamespace(max_photos=5, max_reviews=50, no_photos=False)
        it = {"Id": 77, "Url": "/ho-chi-minh/x"}
        with mock.patch.object(ff, "fetch_place", side_effect=ff.GalleryError("gallery error: album 2: 503")), \
                mock.patch.object(ff, "hb", lambda *x, **k: None):
            self.assertEqual(ff.process_item(None, it, a, st, None), "gallery")
        self.assertNotIn("77", st["done"])
        e = st["gallery_retry"]["77"]
        self.assertEqual(e["tries"], 1)
        self.assertEqual(e["item"]["Url"], "/ho-chi-minh/x")

    def test_retry_backoff_and_give_up_stays_not_done(self):
        st = {"done": {"5": "x"}}
        e1 = ff.queue_gallery_retry(st, "5", {"Id": 5}, "503", now_ts=1000, tries=3, backoff=900, backoff_max=21600)
        self.assertEqual(e1["next_at"], 1900)
        self.assertNotIn("5", st["done"])
        e2 = ff.queue_gallery_retry(st, "5", {"Id": 5}, "503", now_ts=2000, tries=3, backoff=900, backoff_max=21600)
        self.assertEqual(e2["next_at"], 2000 + 1800)
        self.assertEqual(ff.due_gallery_retries(st, now_ts=3000), [])
        self.assertEqual(ff.due_gallery_retries(st, now_ts=4000), ["5"])
        ff.queue_gallery_retry(st, "5", {"Id": 5}, "503", now_ts=4000, tries=3)
        self.assertNotIn("5", st["gallery_retry"])
        self.assertIn("5", st["gallery_failed"])
        self.assertNotIn("5", st["done"])

    def test_success_clears_retry_entry(self):
        st = {"done": {}, "failed": {}, "gallery_retry": {"8": {"tries": 1}}, "gallery_failed": {"8": {}}}
        a = types.SimpleNamespace(max_photos=5, max_reviews=50, no_photos=False)
        rec = {"name": "x", "photos": [], "opening_periods": [], "menu": []}
        root = tempfile.mkdtemp()
        with mock.patch.object(ff, "fetch_place", return_value=rec), mock.patch.object(ff, "ROOT", root), \
                mock.patch.object(ff, "hb", lambda *x, **k: None):
            self.assertEqual(ff.process_item(None, {"Id": 8, "Url": "/x"}, a, st, None, retry=True), "ok")
        self.assertIn("8", st["done"])
        self.assertNotIn("8", st["gallery_retry"]); self.assertNotIn("8", st["gallery_failed"])


class Reviews(unittest.TestCase):  # #1083
    def test_default_cap_is_fifty(self):
        os.environ.pop("AQD_MAX_REVIEWS", None)
        self.assertEqual(importlib.reload(ff).MAX_REVIEWS, 50)

    def test_pages_until_cap(self):
        c = FakeClient(reviews=review_items(547))
        out = ff.reviews(c, 1, "/x", 50)
        self.assertEqual(len(out), 50)
        self.assertEqual(sum(1 for p in c.calls if "ResLoadMore" in p), 5)
        self.assertEqual(len({r["source_id"] for r in out}), 50)
        r = out[0]
        self.assertEqual((r["source"], r["source_id"], r["author"], r["rating"]), ("foody", "9000", "user 0", 7.5))
        self.assertEqual(r["created_at"], "2022-05-06T09:21:11+00:00")
        self.assertTrue(r["source_url"].startswith("https://www.foody.vn/"))

    def test_stops_when_source_runs_out(self):
        c = FakeClient(reviews=review_items(23))
        self.assertEqual(len(ff.reviews(c, 1, "/x", 50)), 23)
        self.assertEqual(sum(1 for p in c.calls if "ResLoadMore" in p), 3)

    def test_env_cap(self):
        with mock.patch.dict(os.environ, {"AQD_MAX_REVIEWS": "12"}):
            self.assertEqual(importlib.reload(ff).MAX_REVIEWS, 12)
        importlib.reload(ff)

    def test_seed_fills_and_dedupes(self):
        c = FakeClient(reviews=review_items(3))
        seed = [{"source": "foody", "source_id": "9000"}, {"source": "foody", "source_id": "1"}]
        out = ff.reviews(c, 1, "/x", 50, seed=seed)
        self.assertEqual([r["source_id"] for r in out], ["9000", "8999", "8998", "1"])

    def test_error_keeps_what_was_read(self):
        c = FakeClient(reviews=review_items(100), review_error_at=3)
        out = ff.reviews(c, 1, "/x", 50, seed=[{"source": "foody", "source_id": "s1"}])
        self.assertEqual(len(out), 21)

    def test_pusher_rows_accept_paged_reviews(self):
        import foody_push
        rows = foody_push.Pusher.review_rows({"source_id": "1", "reviews_sample": [ff.review_row(x) for x in review_items(50)]})
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["rating_original"], 7.5)


if __name__ == "__main__":
    unittest.main()
