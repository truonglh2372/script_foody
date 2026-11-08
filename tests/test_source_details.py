"""web#1390 client side: price/score/description/review title in the import, and a re-crawl "update" mode.

Server contract (an_uong_quanh_day Imports::SourceDetails / RestaurantsCreate): price_min/price_max (int VND >= 0, min <= max),
rating + rating_scale + sub_scores (0..scale), review_count (int >= 0), description (plain text, <= 1000),
mode "update" + fields[] (phone website cuisine category address_district address_city description opening_periods).
No network, no server. Run: python3 tests/test_source_details.py
"""
import io, json, os, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
os.environ["AQD_FOODY_DATA"] = tempfile.mkdtemp(prefix="foody1390_")
os.environ.setdefault("AQD_ROOT", tempfile.mkdtemp(prefix="aqdroot_"))
import foody_upload as fu  # noqa: E402
import foody_push as fp  # noqa: E402


def place(**kw):
    p = {"source_id": "97063", "name": "Quán A", "address": "1 Lê Lợi", "lat": 10.77, "lng": 106.7, "phones": [],
         "opening_periods": [], "menu": [], "photos": [], "reviews_sample": []}
    p.update(kw)
    return p


class DetailsPayloadTest(unittest.TestCase):
    def test_price_rating_description_are_sent(self):
        b = fu.payload(place(price_min=20000.0, price_max=55000.0, rating=6.9, review_count=16,
                             rating_breakdown={"Chất lượng": 7.4, "Giá cả": 6.5},
                             description="<p>Ngon &amp; rẻ</p>\n<br>Mở lâu năm"))
        self.assertEqual((b["price_min"], b["price_max"]), (20000, 55000))
        self.assertEqual((b["rating"], b["rating_scale"], b["review_count"]), (6.9, 10, 16))
        self.assertEqual(b["sub_scores"], {"Chất lượng": 7.4, "Giá cả": 6.5})
        self.assertEqual(b["description"], "Ngon & rẻ Mở lâu năm")

    def test_absent_values_are_left_out(self):
        b = fu.payload(place())
        for k in ("price_min", "price_max", "rating", "rating_scale", "review_count", "sub_scores", "description"):
            self.assertNotIn(k, b)

    def test_invalid_values_are_dropped_not_sent(self):
        b = fu.payload(place(price_min=90000, price_max=10000, rating=11, review_count=-3,
                             rating_breakdown={"Vị trí": 12, "x": "abc"}, description="   "))
        for k in ("price_min", "price_max", "rating", "review_count", "description"):
            self.assertNotIn(k, b, k)
        self.assertNotIn("sub_scores", b)
        self.assertNotIn("rating_scale", b)  # the scale rides only with a rating or sub_scores

    def test_zero_price_is_not_a_price(self):
        b = fu.payload(place(price_min=0, price_max=0))
        self.assertNotIn("price_min", b)
        self.assertNotIn("price_max", b)

    def test_one_sided_price_is_kept(self):
        b = fu.payload(place(price_min=30000))
        self.assertEqual(b["price_min"], 30000)
        self.assertNotIn("price_max", b)

    def test_description_is_capped_at_1000(self):
        self.assertEqual(len(fu.payload(place(description="a" * 2500))["description"]), 1000)

    def test_sub_scores_capped_at_10(self):
        b = fu.payload(place(rating_breakdown={f"k{i}": 5 for i in range(15)}))
        self.assertEqual(len(b["sub_scores"]), 10)


class ReviewTitleTest(unittest.TestCase):
    def test_title_is_sent_trimmed_to_200(self):
        rows = fp.Pusher.review_rows(place(reviews_sample=[
            {"source_id": "1", "rating": 8, "title": "  Ngon  ", "text": "x"},
            {"source_id": "2", "rating": 8, "title": "t" * 300, "text": "x"},
            {"source_id": "3", "rating": 8, "title": "", "text": "x"}]))
        self.assertEqual(rows[0]["title"], "Ngon")
        self.assertEqual(len(rows[1]["title"]), 200)
        self.assertNotIn("title", rows[2])


class UpdateModeTest(unittest.TestCase):
    def test_update_body_has_mode_and_fields(self):
        b = fu.update_payload(place(description="Mô tả"), ["description", "opening_periods"])
        self.assertEqual(b["mode"], "update")
        self.assertEqual(b["fields"], ["description", "opening_periods"])
        self.assertEqual(b["source"], "foody")
        self.assertEqual(b["source_id"], "97063")

    def test_unknown_or_empty_fields_are_refused_before_any_call(self):
        with self.assertRaises(ValueError):
            fu.update_payload(place(), [])
        with self.assertRaises(ValueError):
            fu.update_payload(place(), ["menu"])  # the server cannot update menu

    def pusher(self, resp):
        pu = fp.Pusher.__new__(fp.Pusher)
        pu.api = mock.Mock()
        pu.api.call.return_value = resp
        pu.done = {"97063": {"restaurant_id": 7}}
        return pu

    def write(self, fid="97063", **kw):
        os.makedirs(os.path.join(fp.ROOT, fid), exist_ok=True)
        json.dump(place(source_id=fid, **kw), open(os.path.join(fp.ROOT, fid, "place.json"), "w", encoding="utf-8"))

    def test_update_existing_posts_update_and_reports_changes(self):
        self.write(description="Mô tả mới")
        pu = self.pusher({"restaurant_id": 7, "created": False,
                          "changes": {"applied": ["description"],
                                      "skipped": [{"field": "opening_periods", "reason": "edited_after_import"}]}})
        out = io.StringIO()
        st = fp.update_existing(pu, ["97063"], ["description", "opening_periods"], apply=True,
                                out=lambda m: out.write(m + "\n"))
        body = pu.api.call.call_args.kwargs["json_body"]
        self.assertEqual((body["mode"], body["fields"]), ("update", ["description", "opening_periods"]))
        self.assertEqual((st["applied"], st["skipped"], st["fail"]), (1, 1, 0))
        self.assertIn("edited_after_import", out.getvalue())

    def test_dry_run_calls_nothing(self):
        self.write()
        pu = self.pusher({})
        st = fp.update_existing(pu, ["97063"], ["description"], apply=False, out=lambda m: None)
        pu.api.call.assert_not_called()
        self.assertEqual(st["places"], 1)

    def test_other_restaurant_id_is_not_trusted(self):
        self.write()
        pu = self.pusher({"restaurant_id": 99, "created": True})
        st = fp.update_existing(pu, ["97063"], ["description"], apply=True, out=lambda m: None)
        self.assertEqual(st["mismatch"], 1)

    def test_only_already_imported_places_are_updated(self):
        self.write("555")
        pu = self.pusher({})
        st = fp.update_existing(pu, ["555"], ["description"], apply=True, out=lambda m: None)
        pu.api.call.assert_not_called()
        self.assertEqual(st["places"], 0)

    def test_server_error_counts_as_fail_and_continues(self):
        self.write()
        pu = self.pusher({})
        pu.api.call.side_effect = RuntimeError("boom")
        st = fp.update_existing(pu, ["97063"], ["description"], apply=True, out=lambda m: None)
        self.assertEqual(st["fail"], 1)


if __name__ == "__main__":
    unittest.main()
