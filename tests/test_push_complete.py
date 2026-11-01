"""script_foody#7: everything the crawler collects reaches the server.

  1  string-form API errors ({"status":404,"error":"Not Found"}) raise APIError with the real status, not AttributeError;
     cleanup/review_rows tolerate non-dict items; Pusher.handle prints the full traceback.
  2  phones: list_phone / phone / phones merged and normalised to Vietnamese numbers.
  3  fields the import API does not accept (price, score, description) are NOT invented in the payload.
  4  review payload: published_at (ISO 8601), no title field, at most AQD_MAX_REVIEWS (50).
  5  menu: window.intMenu read whatever follows the object.
  6  opening hours: Foody's after-midnight tail (00:05-01:00) is folded into the previous evening's shift.
  7  backfill mode: re-sends import + missing reviews for places already created, never creates a new place.
No network, no server. Run: python3 tests/test_push_complete.py
"""
import io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
DATA = tempfile.mkdtemp(prefix="foody7_")
os.environ["AQD_FOODY_DATA"] = DATA
os.environ.setdefault("AQD_ROOT", tempfile.mkdtemp(prefix="aqdroot_"))
import aqd_api  # noqa: E402
import foody_upload as fu  # noqa: E402
import foody_push as fp  # noqa: E402
import foody_fetch as ff  # noqa: E402


class FakeResp:
    def __init__(self, status, body, reason="Not Found"):
        self.status_code, self._body, self.reason, self.headers = status, body, reason, {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def api_with(resp):
    api = aqd_api.AQD.__new__(aqd_api.AQD)
    api.base = "https://example.invalid"; api.verbose = False
    api.tokens = mock.Mock(); api.tokens.access.return_value = "t"
    sess = mock.Mock(); sess.request.return_value = resp
    api._tl = mock.Mock(); api._tl.s = sess
    return api


def place(**kw):
    p = {"source_id": "873000", "name": "Quán A", "address": "1 Lê Lợi", "lat": 10.7, "lng": 106.7,
         "phones": [], "list_phone": "0982 436 061 - (028) 2224 0361", "price_min": 20000.0, "price_max": 55000.0,
         "rating": 6.9, "review_count": 16, "rating_breakdown": {"Chất lượng": 7.4}, "description": "Ngon",
         "opening_periods": [], "menu": [], "photos": [], "reviews_sample": []}
    p.update(kw)
    return p


class StringErrorTest(unittest.TestCase):
    def test_rails_404_string_error_becomes_apierror(self):
        api = api_with(FakeResp(404, {"status": 404, "error": "Not Found"}))
        with self.assertRaises(aqd_api.APIError) as cm:
            api.call("POST", "/v1/admin/imports/restaurants/96989/reviews", json_body={}, retries=0)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.code, "Not Found")
        self.assertIn("/reviews", str(cm.exception))

    def test_dict_error_and_odd_bodies(self):
        self.assertEqual(aqd_api.error_fields({"error": {"code": "x", "details": {"a": 1}}}), ("x", {"a": 1}))
        self.assertEqual(aqd_api.error_fields(["oops"], "Bad"), ("Bad", None))
        self.assertEqual(aqd_api.error_fields(None, "Bad"), ("Bad", None))
        self.assertEqual(aqd_api.error_fields({"errors": ["e"]}, "Bad"), ("Bad", {"errors": ["e"]}))
        api = api_with(FakeResp(502, ValueError("html")))
        with self.assertRaises(aqd_api.APIError) as cm:
            api.call("GET", "/x", retries=0)
        self.assertEqual(cm.exception.status, 502)

    def test_handle_prints_full_traceback(self):
        pu = fp.Pusher.__new__(fp.Pusher)
        pu.stats = {"fail": 0}
        pu.push = mock.Mock(side_effect=aqd_api.APIError(404, "Not Found", None, "POST /r"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertFalse(pu.handle("873000"))
        self.assertIn("Traceback (most recent call last)", buf.getvalue())
        self.assertIn("APIError 404", buf.getvalue())

    def test_string_items_in_photos_and_reviews_do_not_crash(self):
        p = place(reviews_sample=["text only", {"source_id": "1", "rating": 8, "text": "ok"}],
                  photos=["a.jpg", {"keep": True, "file": "photos/1.jpg", "source_id": "9", "role": "menu"}])
        self.assertEqual([r["source_id"] for r in fp.Pusher.review_rows(p)], ["1"])
        self.assertEqual([x["source_id"] for x in fu.photos_of(p)], ["9"])


class PhoneTest(unittest.TestCase):
    def test_list_phone_mapped_and_normalised(self):
        self.assertEqual(fu.payload(place())["phones"], ["0982436061", "02822240361"])

    def test_variants(self):
        n = fu.normalize_vn_phone
        self.assertEqual(n("+84 909 123 456"), "0909123456")
        self.assertEqual(n("84909123456"), "0909123456")
        self.assertEqual(n("(028) 38 332 748"), "02838332748")
        self.assertEqual(n("1900 6868"), "19006868")
        self.assertIsNone(n("Đang cập nhật")); self.assertIsNone(n("12345")); self.assertIsNone(n(""))

    def test_merge_dedupes_sources(self):
        p = place(phones=["0909123456"], phone="+84909123456", list_phone="Đang cập nhật")
        self.assertEqual(fu.phones_of(p), ["0909123456"])
        self.assertEqual(fu.phones_of(place(list_phone="Đang cập nhật")), [])


class FieldsTest(unittest.TestCase):
    def test_only_fields_the_import_api_permits(self):
        # Api::V1::Admin::ImportRestaurantsController#payload permits exactly these keys.
        permitted = {"source", "source_id", "source_url", "name", "address", "district", "city", "lat", "lng",
                     "website", "phones", "cuisines", "categories", "opening_periods", "menu"}
        body = fu.payload(place(source_url="https://www.foody.vn/x", district="Q1", city="HCM"))
        self.assertLessEqual(set(body), permitted)
        for k in ("price_min", "price_max", "rating", "review_count", "rating_breakdown", "description"):
            self.assertNotIn(k, body)


class ReviewPayloadTest(unittest.TestCase):
    def test_review_rows(self):
        p = place(reviews_sample=[{"source_id": "11", "rating": 8.5, "title": "Ngon", "text": "Bún ngon",
                                   "author": "An", "created_at": "2026-10-17T09:30:00+00:00",
                                   "source_url": "https://www.foody.vn/r/11"},
                                  {"source_id": "12", "rating": 6, "text": "", "created_at": "/Date(1760000000000)/"},
                                  {"source_id": "13", "rating": 7, "created_at": "không rõ"},
                                  {"source_id": "14", "rating": 99}])
        rows = fp.Pusher.review_rows(p)
        self.assertEqual([r["source_id"] for r in rows], ["11", "12", "13"])
        self.assertEqual(rows[0], {"source": "foody", "source_id": "11", "source_url": "https://www.foody.vn/r/11",
                                   "author_name": "An", "rating_original": 8.5, "text": "Bún ngon",
                                   "published_at": "2026-10-17T09:30:00+00:00"})
        self.assertEqual(rows[1]["published_at"], "2025-10-09T08:53:20+00:00")
        self.assertNotIn("published_at", rows[2])  # unreadable date is dropped, never sent malformed
        self.assertNotIn("title", rows[0])

    def test_local_date_gets_vn_offset(self):
        self.assertEqual(fu.iso_time("17/10/2026 21:05"), "2026-10-17T21:05:00+07:00")

    def test_cap_50(self):
        p = place(reviews_sample=[{"source_id": str(i), "rating": 7} for i in range(80)])
        self.assertEqual(len(fp.Pusher.review_rows(p)), 50)
        self.assertEqual(len(fu.reviews_of(p)), 50)


class MenuParseTest(unittest.TestCase):
    MENU = {"MenuItems": [{"Name": "Bún", "Dishes": [{"Name": "Bún bò", "Price": 45000}]}]}

    def test_minified_page(self):
        for tail in (";</script>", "; window.other = 1;", ";\n", ""):
            html = "<script>window.intMenu = " + json.dumps(self.MENU) + tail
            self.assertEqual(ff.parse_int_menu(html), self.MENU, tail)

    def test_menu_items(self):
        c = mock.Mock(); c.get.return_value = mock.Mock(text="<script>window.intMenu=" + json.dumps(self.MENU) + ";</script>")
        self.assertEqual(ff.menu(c, 1, "/ho-chi-minh/x")[0]["name"], "Bún bò")
        c.get.return_value = mock.Mock(text="<html>no menu</html>")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ff.menu(c, 1, "/ho-chi-minh/x"), [])


class OpeningTest(unittest.TestCase):
    @staticmethod
    def week(*slots):
        return [{"weekday": d, "opens_at": a, "closes_at": b} for d in range(1, 8) for a, b in slots]

    def test_tail_folded_into_previous_evening(self):
        out = fu.clean_periods(self.week(("00:05", "01:00"), ("17:00", "23:59")))
        self.assertEqual(len(out), 7)
        self.assertTrue(all((x["opens_at"], x["closes_at"]) == ("17:00", "01:00") for x in out))

    def test_tail_without_evening_kept(self):
        per = [{"weekday": 2, "opens_at": "00:05", "closes_at": "01:00"},
               {"weekday": 2, "opens_at": "17:00", "closes_at": "23:59"}]
        out = fu.clean_periods(per)
        self.assertEqual(sorted((x["weekday"], x["opens_at"]) for x in out), [(2, "00:05"), (2, "17:00")])
        self.assertEqual(fu.clean_periods([{"weekday": 3, "opens_at": "00:01", "closes_at": "04:00"}])[0]["opens_at"], "00:01")

    def test_normal_day_unchanged(self):
        out = fu.clean_periods(self.week(("07:00", "14:00"), ("16:00", "21:00")))
        self.assertEqual(len(out), 14)


class BackfillTest(unittest.TestCase):
    def setUp(self):
        os.makedirs(os.path.join(DATA, "873000"), exist_ok=True)
        p = place(reviews_sample=[{"source_id": str(i), "rating": 8, "text": "t"} for i in range(3)])
        with open(os.path.join(DATA, "873000", "place.json"), "w") as f:
            json.dump(p, f)
        self.pu = fp.Pusher.__new__(fp.Pusher)
        self.pu.done = {"873000": {"restaurant_id": 96989, "photos": []}, "999": {"photos": []}}
        self.pu.cleaned = {}; self.pu.rpushed = {"873000": ["0"]}
        self.pu.stats = {"reviews": 0, "pushed": 0, "photos": 0, "deleted_files": 0, "freed_bytes": 0, "fail": 0}
        self.pu._save = lambda: None
        self.pu.api = mock.Mock()

    def test_dry_run_calls_nothing(self):
        st = fp.backfill(self.pu, apply=False, out=lambda *_: None)
        self.pu.api.call.assert_not_called()
        self.assertEqual(st["places"], 1); self.assertEqual(st["reviews"], 2)

    def test_apply_refills_and_pushes_only_missing_reviews(self):
        self.pu.api.call.side_effect = lambda m, path, **kw: (
            {"restaurant_id": 96989, "created": False} if path == fp.EP else
            {"created": 2} if path.endswith("/reviews") else {"photos": [], "has_more": False})
        st = fp.backfill(self.pu, apply=True, out=lambda *_: None)
        calls = self.pu.api.call.call_args_list
        self.assertEqual(calls[0].args[:2], ("POST", fp.EP))
        self.assertEqual(calls[0].kwargs["json_body"]["phones"], ["0982436061", "02822240361"])
        rv = [c for c in calls if c.args[1].endswith("/reviews")]
        self.assertEqual(rv[0].args[1], f"{fp.EP}/96989/reviews")
        self.assertEqual([r["source_id"] for r in rv[0].kwargs["json_body"]["reviews"]], ["1", "2"])
        self.assertEqual(st["refilled"], 1); self.assertEqual(st["reviews"], 2)
        self.assertEqual(self.pu.done["873000"]["restaurant_id"], 96989)
        self.assertNotIn("999", [c.args[1] for c in calls])

    def test_mismatched_restaurant_is_not_overwritten(self):
        self.pu.api.call.return_value = {"restaurant_id": 12345, "created": True}
        st = fp.backfill(self.pu, apply=True, out=lambda *_: None)
        self.assertEqual(st["mismatch"], 1)
        self.assertEqual(self.pu.done["873000"]["restaurant_id"], 96989)
        self.assertEqual(len(self.pu.api.call.call_args_list), 1)  # no reviews pushed to a wrong place


if __name__ == "__main__":
    unittest.main()
