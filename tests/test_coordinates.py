"""script_foody#18 (an_uong_quanh_day#1452): never send a Foody point that cannot be in Vietnam.

Foody's own page data is wrong for some places (itemprop latitude/longitude on the page itself). The crawler used to pass
it on after a +-90/+-180 check, so (0,0), (1,2), lat=lng and 0.75... (a leading "1" lost) reached the import API.
Real broken inputs below come from the 11 production rows. No network, no server.
Run: python3 tests/test_coordinates.py
"""
import json, os, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
DATA = tempfile.mkdtemp(prefix="foody18_")
os.environ["AQD_FOODY_DATA"] = DATA
os.environ.setdefault("AQD_ROOT", tempfile.mkdtemp(prefix="aqdroot_"))
import aqd_api  # noqa: E402
import foody_upload as fu  # noqa: E402
import foody_push as fp  # noqa: E402

BROKEN = {  # restaurant id: (lat, lng) exactly as stored in production
    103921: (10.7720113922, 10.7720113922),  # lat copied into lng
    138077: (10.7433766, 10.7433766),
    108846: (1.0, 2.0),  # test row
    118971: (0.0, 0.0),
    131548: (0.0, 106.635575),
    129925: (0.755671, 106.722071),  # 10.755671 minus its leading "1"
    144747: (0.768165, 106.667575),
}
GOOD = [(10.755671, 106.722071), (10.823099, 106.629664), (21.0285, 105.8542), (12.0075696, 109.214995)]


def place(**kw):
    p = {"source_id": "129925", "name": "Cơm tấm Chú Thái", "address": "36 Huỳnh Tấn Phát", "lat": 10.755671,
         "lng": 106.722071, "phones": [], "opening_periods": [], "menu": [], "photos": [], "reviews_sample": []}
    p.update(kw)
    return p


class CoordsProblemTest(unittest.TestCase):
    def test_every_real_broken_point_is_rejected(self):
        for rid, (lat, lng) in BROKEN.items():
            self.assertIsNotNone(fu.coords_problem(lat, lng), f"{rid}: ({lat}, {lng}) must be rejected")

    def test_reasons(self):
        self.assertEqual(fu.coords_problem(10.7433766, 10.7433766), "coords_lat_equals_lng")
        self.assertEqual(fu.coords_problem(0.0, 0.0), "coords_out_of_vietnam")
        self.assertEqual(fu.coords_problem(0.755671, 106.722071), "coords_out_of_vietnam")
        self.assertEqual(fu.coords_problem(None, 106.7), "coords")
        self.assertEqual(fu.coords_problem("x", 106.7), "coords")
        self.assertEqual(fu.coords_problem(float("nan"), 106.7), "coords")

    def test_valid_points_pass(self):
        for lat, lng in GOOD:
            self.assertIsNone(fu.coords_problem(lat, lng))
        self.assertIsNone(fu.coords_problem("10.755671", "106.722071"))  # numeric strings are fine

    def test_problems_lists_the_reason(self):
        for rid, (lat, lng) in BROKEN.items():
            body = fu.payload(place(lat=lat, lng=lng))
            self.assertTrue(any(x.startswith("coords") for x in fu.problems(body)), rid)
        self.assertEqual(fu.problems(fu.payload(place())), [])

    def test_missing_coordinates_are_a_problem_not_zero(self):
        body = fu.payload(place(lat=None, lng=None))
        self.assertNotIn("lat", body)
        self.assertEqual(fu.problems(body), ["coords"])


class PusherTest(unittest.TestCase):
    def pusher(self):
        pu = fp.Pusher.__new__(fp.Pusher)
        pu.api = mock.Mock()
        pu.done, pu.cleaned, pu.rpushed, pu.bad = {}, {}, {}, {}
        pu.stats = {"reviews": 0, "pushed": 0, "photos": 0, "deleted_files": 0, "freed_bytes": 0, "fail": 0}
        return pu

    def write_place(self, fid, **kw):
        os.makedirs(os.path.join(fp.ROOT, fid), exist_ok=True)
        json.dump(place(source_id=fid, **kw), open(os.path.join(fp.ROOT, fid, "place.json"), "w", encoding="utf-8"))

    def test_bad_point_is_not_sent_and_is_flagged(self):
        self.write_place("129925", lat=0.755671, lng=106.722071)
        pu = self.pusher()
        self.assertFalse(pu.push("129925"))
        pu.api.call.assert_not_called()
        self.assertEqual(pu.bad["129925"]["problems"], ["coords_out_of_vietnam"])
        saved = json.load(open(os.path.join(fp.ROOT, "_bad_coords.json"), encoding="utf-8"))
        self.assertEqual(saved["129925"]["lat"], 0.755671)

    def test_server_422_is_flagged_not_retried_and_not_a_failure(self):
        self.write_place("500001")
        pu = self.pusher()
        pu.api.call.side_effect = aqd_api.APIError(422, "import_coordinates_out_of_range", None, "POST /x")
        self.assertFalse(pu.push("500001"))
        self.assertEqual(pu.api.call.call_count, 1)
        self.assertEqual(pu.bad["500001"]["problems"], ["server_import_coordinates_out_of_range"])
        pu.bad.clear()
        pu.handle("500001")
        self.assertEqual(pu.stats["fail"], 0)

    def test_other_422_still_raises(self):
        self.write_place("500002")
        pu = self.pusher()
        pu.api.call.side_effect = aqd_api.APIError(422, "import_name_required", None, "POST /x")
        with self.assertRaises(aqd_api.APIError):
            pu.push("500002")
        self.assertEqual(pu.bad, {})

    def test_valid_point_clears_an_old_flag_and_is_sent(self):
        self.write_place("500003")
        pu = self.pusher()
        pu.bad["500003"] = {"problems": ["coords_out_of_vietnam"]}
        pu.api.call.return_value = {"restaurant_id": 7, "created": True}
        self.assertTrue(pu.push("500003"))
        self.assertNotIn("500003", pu.bad)

    def test_backlog_skips_flagged_places(self):
        self.write_place("500004", lat=0.0, lng=0.0)
        pu = self.pusher()
        pu.bad["500004"] = {"problems": ["coords_out_of_vietnam"]}
        self.assertNotIn("500004", pu.backlog())


if __name__ == "__main__":
    unittest.main()
