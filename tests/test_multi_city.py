"""Multi-city state + permanent import 422 skip. No network.
Run: python3 tests/test_multi_city.py
"""
import json, os, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
DATA = tempfile.mkdtemp(prefix="foody_cities_")
os.environ["AQD_FOODY_DATA"] = DATA
os.environ.setdefault("AQD_ROOT", tempfile.mkdtemp(prefix="aqdroot_"))

import foody_cities as fc  # noqa: E402
import foody_fetch as ff  # noqa: E402
import foody_push as fp  # noqa: E402
import foody_upload as fu  # noqa: E402
from aqd_api import APIError  # noqa: E402


class MigrateStateTest(unittest.TestCase):
    def test_legacy_flat_becomes_hcm_bucket(self):
        st = {"page": 17, "done": {"1": "x"}, "failed": {}, "total": 100, "slice": 2, "slices": [{"district": 1, "cate": "", "total": 10}]}
        out = fc.migrate_state(st)
        self.assertEqual(out["schema"], 2)
        self.assertEqual(out["city"], "ho-chi-minh")
        self.assertEqual(out["cities"]["ho-chi-minh"]["page"], 17)
        self.assertEqual(out["cities"]["ho-chi-minh"]["done"], {"1": "x"})
        self.assertNotIn("done", out)
        # idempotent
        out2 = fc.migrate_state(out)
        self.assertEqual(out2["cities"]["ho-chi-minh"]["page"], 17)

    def test_filter_and_seed(self):
        seed = fc.seed_cities()
        self.assertGreaterEqual(len(seed), 60)
        self.assertTrue(all(c["slug"] != "toan-quoc" for c in seed))
        only = fc.filter_cities(seed, "ha-noi,217")
        slugs = {c["slug"] for c in only}
        self.assertEqual(slugs, {"ha-noi", "ho-chi-minh"})

    def test_advance_city_wraps(self):
        st = fc.migrate_state({"page": 1, "done": {}, "failed": {}})
        st["city_order"] = ["ho-chi-minh", "ha-noi"]
        st["city_index"] = 0
        st["city"] = "ho-chi-minh"
        fc.ensure_city_bucket(st, "ha-noi")
        slug, wrapped = fc.advance_city(st, st["city_order"])
        self.assertEqual(slug, "ha-noi")
        self.assertFalse(wrapped)
        slug, wrapped = fc.advance_city(st, st["city_order"])
        self.assertEqual(slug, "ho-chi-minh")
        self.assertTrue(wrapped)


class PermanentImportTest(unittest.TestCase):
    def pusher(self):
        pu = fp.Pusher.__new__(fp.Pusher)
        pu.api = mock.Mock()
        pu.done, pu.cleaned, pu.rpushed, pu.bad = {}, {}, {}, {}
        pu.stats = {"reviews": 0, "pushed": 0, "photos": 0, "deleted_files": 0, "freed_bytes": 0, "fail": 0}
        pu._save = lambda: None
        return pu

    def write_place(self, fid, **kw):
        os.makedirs(os.path.join(fp.ROOT, fid), exist_ok=True)
        p = {"source_id": fid, "name": "Test", "address": "1 St", "lat": 10.755671, "lng": 106.722071,
             "phones": [], "opening_periods": [{"weekday": 1, "opens_at": "08:00", "closes_at": "22:00"}],
             "menu": [], "photos": [], "reviews_sample": []}
        p.update(kw)
        json.dump(p, open(os.path.join(fp.ROOT, fid, "place.json"), "w", encoding="utf-8"))

    def test_opening_periods_invalid_is_permanent(self):
        pu = self.pusher()
        self.write_place("5357")
        pu.api.call.side_effect = APIError(422, "opening_periods_invalid", {}, "POST /v1/admin/imports/restaurants")
        self.assertFalse(pu.push("5357"))
        self.assertIn("5357", pu.bad)
        self.assertEqual(pu.bad["5357"]["problems"], ["server_opening_periods_invalid"])
        pu.api.call.reset_mock()
        self.assertFalse(pu.push("5357"))  # second call must not hit API
        pu.api.call.assert_not_called()

    def test_coords_out_of_range_is_permanent(self):
        pu = self.pusher()
        self.write_place("312175")
        pu.api.call.side_effect = APIError(422, "import_coordinates_out_of_range", {}, "POST ...")
        self.assertFalse(pu.push("312175"))
        self.assertIn("312175", pu.bad)
        pu.api.call.reset_mock()
        self.assertFalse(pu.push("312175"))
        pu.api.call.assert_not_called()

    def test_backlog_skips_bad(self):
        pu = self.pusher()
        self.write_place("1")
        self.write_place("2")
        pu.bad["1"] = {"problems": ["server_opening_periods_invalid"]}
        bl = pu.backlog()
        self.assertNotIn("1", bl)
        self.assertIn("2", bl)


class ActiveBucketTest(unittest.TestCase):
    def test_active_bucket_uses_city(self):
        st = fc.migrate_state({"page": 3, "done": {"9": 1}, "failed": {}})
        st["city_order"] = ["ho-chi-minh", "ha-noi"]
        fc.ensure_city_bucket(st, "ha-noi")
        st["city"] = "ha-noi"
        st["city_index"] = 1
        b = ff._active_bucket(st)
        self.assertEqual(b.get("page"), 1)
        self.assertEqual(b.get("done"), {})



class DistrictSlicesTest(unittest.TestCase):
    def test_build_slices_uses_provided_district_ids(self):
        # Simulate Hà Nội-like ids (20+) and a high id (690) that the 1..80 probe would miss.
        totals = {
            (20, ""): 100,
            (21, ""): 2500,  # over LIST_CAP -> split by cate
            (690, ""): 50,
            ("", 1): 10,
            (21, 1): 40,
        }
        def total_of(d, k):
            return totals.get((d, k), 0)
        out = ff.build_slices(total_of, cap=2000, max_id=80, district_ids=[20, 21, 690])
        districts = {s["district"] for s in out}
        self.assertEqual(districts, {20, 21, 690})
        # 21 over cap -> cate slice
        self.assertTrue(any(s["district"] == 21 and s["cate"] == 1 for s in out))
        # probe fallback still works without district_ids (legacy)
        out2 = ff.build_slices(total_of, cap=2000, max_id=25)
        self.assertIn(20, {s["district"] for s in out2})
        self.assertNotIn(690, {s["district"] for s in out2})

    def test_fetch_district_ids_parses_api(self):
        class FakeClient:
            city = "ha-noi"
            city_id = 218
            def json(self, path, referer=None):
                self.last = path
                return {"data": [{"Id": 20, "Name": "Ba Đình"}, {"Id": 690, "Name": "Bắc Từ Liêm"},
                                 {"Id": 20, "Name": "dup"}]}
        c = FakeClient()
        ids = ff.fetch_district_ids(c)
        self.assertEqual(ids, [20, 690])
        self.assertIn("GetDistrictsByProvinceId", c.last)
        self.assertIn("provinceId=218", c.last)


class SkipCitiesTest(unittest.TestCase):
    def test_resolve_and_skip_preserves_hcm_bucket(self):
        st = fc.migrate_state({"page": 9, "done": {"1": "x"}, "failed": {}, "slice": 3,
                               "slices": [{"district": 1, "cate": "", "total": 10}]})
        st["city_order"] = ["ho-chi-minh", "ha-noi", "da-nang"]
        fc.ensure_city_bucket(st, "ha-noi")
        fc.ensure_city_bucket(st, "da-nang")
        st["city"] = "ho-chi-minh"
        st["city_index"] = 0
        skip = fc.resolve_skip_cities(st, cli="ho-chi-minh", env=None, seed=fc.seed_cities())
        self.assertEqual(skip, {"ho-chi-minh"})
        self.assertEqual(st["skip_cities"], ["ho-chi-minh"])
        slug, wrapped = fc.skip_current_cities(st, st["city_order"], skip)
        self.assertEqual(slug, "ha-noi")
        self.assertFalse(wrapped)
        # HCM bucket untouched
        self.assertEqual(st["cities"]["ho-chi-minh"]["page"], 9)
        self.assertEqual(st["cities"]["ho-chi-minh"]["done"], {"1": "x"})
        self.assertEqual(st["cities"]["ho-chi-minh"]["slice"], 3)

    def test_skip_by_id(self):
        st = {"schema": 2, "cities": {}, "city_order": ["ho-chi-minh", "ha-noi"], "city": "ho-chi-minh", "city_index": 0}
        skip = fc.resolve_skip_cities(st, cli="217", env=None, seed=fc.seed_cities())
        self.assertEqual(skip, {"ho-chi-minh"})

if __name__ == "__main__":
    unittest.main(verbosity=2)
