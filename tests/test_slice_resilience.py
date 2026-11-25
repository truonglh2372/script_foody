import json, os, sys, tempfile, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
os.environ.setdefault("AQD_FOODY_DATA", tempfile.mkdtemp(prefix="foody_test_"))
import foody_cities as fc  # noqa: E402
import foody_fetch as ff  # noqa: E402


class FakeClient:
    def __init__(self, city="vung-tau", city_id=223, served=None, districts=None, totals=None, list_errors=None,
                 district_errors=0, on_set_city=None):
        self.city, self.city_id, self.lat, self.lng = city, city_id, 10.0, 107.0
        self.served = served or {}
        self.districts = districts
        self.totals = totals or {}
        self.list_errors = dict(list_errors or {})
        self.district_errors = district_errors
        self.on_set_city = on_set_city
        self.calls = []

    def set_city(self, city):
        self.city, self.city_id = city["slug"], int(city["id"])
        if self.on_set_city:
            self.on_set_city(self)

    def json(self, path, referer=None):
        self.calls.append(path)
        if "GetDistrictsByProvinceId" in path:
            if self.district_errors > 0:
                self.district_errors -= 1
                raise RuntimeError("bỏ cuộc sau 6 lần")
            return {"data": [{"Id": d} for d in self.districts or []]}
        if "HomeListPlace" in path:
            q = dict(x.split("=", 1) for x in path.split("?", 1)[1].split("&"))
            d = int(q["districtId"]) if q["districtId"] else ""
            k = int(q["cateId"]) if q["cateId"] else ""
            n = self.list_errors.get((d, k), 0)
            if n > 0:
                self.list_errors[(d, k)] = n - 1
                raise RuntimeError("503 busy")
            served = self.served.get(self.city_id, self.city_id)
            items = [{"Id": 1001, "Url": "/x/1001"}] if (d, k) == ("", "") else []
            return {"Items": items, "Total": self.totals.get((d, k), 0), "CityId": served}
        raise AssertionError(path)


def crawl_args():
    return types.SimpleNamespace(limit=0, parity="all", refetch=False, min_free_gb=0)


def multi_state(order, city, **bucket):
    st = {"schema": 2, "cities": {}, "city_order": list(order), "city": city, "city_index": order.index(city)}
    for s in order:
        fc.ensure_city_bucket(st, s)
    st["cities"][city].update(bucket)
    return st


class SliceResilienceAndCityMismatchTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="foody_slice_")
        self.stp = os.path.join(self.root, "_state.json")
        self.sleeps = []
        patches = [mock.patch.object(ff, "ROOT", self.root), mock.patch.object(ff, "HB", mock.Mock()),
                   mock.patch.object(ff.time, "sleep", self.sleeps.append),
                   mock.patch.object(ff, "SLICE_PROBE_RETRIES", 3), mock.patch.object(ff, "SLICE_PROBE_BACKOFF", 5),
                   mock.patch.object(ff, "SLICE_MAX_ID", 3), mock.patch.object(ff, "LIST_CAP", 2000)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def stop(self, *_):
        open(os.path.join(self.root, "STOP"), "w").close()

    def hb_errors(self, code):
        return [x for x in ff.HB.error.call_args_list if x.args and x.args[0] == code]

    def test_list_page_raises_on_city_mismatch(self):
        c = FakeClient(city="vung-tau", city_id=223, served={223: 217}, totals={("", ""): 37191})
        with self.assertRaises(ff.CityMismatchError) as cm:
            ff.list_page(c, 1)
        self.assertEqual((cm.exception.expected, cm.exception.got), (223, 217))

    def test_list_page_accepts_matching_or_missing_city_id(self):
        c = FakeClient(totals={("", ""): 500})
        self.assertEqual(ff.list_page(c, 1)[1], 500)
        c.json = lambda path, referer=None: {"Items": [{"Id": 1}], "Total": 9}
        self.assertEqual(ff.list_page(c, 1), ([{"Id": 1}], 9))

    def test_crawl_skips_mismatched_city_without_crawling_items(self):
        c = FakeClient(city="vung-tau", city_id=223, served={223: 217}, totals={("", ""): 37191},
                       on_set_city=self.stop)
        st = multi_state(["vung-tau", "lam-dong"], "vung-tau", page=1)
        with mock.patch.object(ff, "process_item") as proc:
            ff._crawl(crawl_args(), st, self.stp, None, c, 1)
        proc.assert_not_called()
        self.assertEqual(st["city"], "lam-dong")
        self.assertEqual(c.city_id, 254)
        bucket = st["cities"]["vung-tau"]
        self.assertEqual(bucket["city_mismatch"]["got"], 217)
        self.assertEqual(bucket["done"], {})
        self.assertEqual(len(self.hb_errors("foody_city_mismatch")), 1)
        self.assertTrue(self.hb_errors("foody_city_mismatch")[0].kwargs.get("severe"))
        with open(self.stp, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["city"], "lam-dong")

    def test_build_slices_live_propagates_city_mismatch_without_retry(self):
        c = FakeClient(city="vung-tau", city_id=223, served={223: 217}, districts=[1220, 1221])
        with self.assertRaises(ff.CityMismatchError):
            ff._build_slices_live(c, known_total=37191)
        self.assertEqual(sum(1 for p in c.calls if "HomeListPlace" in p), 1)

    def test_probe_retry_backoff_then_success(self):
        seq = [RuntimeError("503"), None, 42]

        def fn():
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        self.assertEqual(ff.probe_retry(fn, "x", retries=3, backoff=5, backoff_max=600), 42)
        self.assertEqual(self.sleeps, [5, 10])

    def test_probe_retry_gives_up(self):
        with self.assertRaises(ff.SliceProbeError):
            ff.probe_retry(lambda: None, "x", retries=2, backoff=1)
        self.assertEqual(self.sleeps, [1])

    def test_total_probe_errors_are_retried_not_zero(self):
        c = FakeClient(districts=[50, 51], totals={(50, ""): 800, (51, ""): 300},
                       list_errors={(50, ""): 2})
        out = ff._build_slices_live(c, known_total=1100)
        self.assertEqual(out, [{"district": 50, "cate": "", "total": 800}, {"district": 51, "cate": "", "total": 300}])
        self.assertEqual(self.sleeps, [5, 10])

    def test_fetch_district_ids_retries(self):
        c = FakeClient(districts=[246, 247], district_errors=2)
        stats = {"errors": 0}
        self.assertEqual(ff.fetch_district_ids(c, stats=stats), [246, 247])
        self.assertEqual(stats["errors"], 0)
        c = FakeClient(districts=[246], district_errors=5)
        self.assertEqual(ff.fetch_district_ids(c, stats=stats), [])
        self.assertEqual(stats["errors"], 1)

    def test_build_slices_cate_fallback(self):
        totals = {("", 1): 1500, ("", 3): 900}
        out = ff.build_slices(lambda d, k: totals.get((d, k), 0), cap=2000, max_id=3, district_ids=[1220, 1221])
        self.assertEqual(out, [{"district": "", "cate": 1, "total": 1500}, {"district": "", "cate": 3, "total": 900}])
        self.assertEqual(ff.build_slices(lambda d, k: totals.get((d, k), 0), cap=2000, max_id=3,
                                         district_ids=[1220], cate_fallback=False), [])
        self.assertEqual(ff.slice_kw({"slice": 1, "slices": out}), {"district": "", "cate": 1})

    def test_build_slices_live_cate_fallback_for_valid_city(self):
        c = FakeClient(districts=[1220, 1221], totals={("", ""): 2500, ("", 2): 2500})
        out = ff._build_slices_live(c, known_total=2500)
        self.assertEqual(out, [{"district": "", "cate": 2, "total": 2500}])

    def test_zero_slices_over_cap_raises_slice_build_error(self):
        c = FakeClient(districts=[1220, 1221])
        with self.assertRaises(ff.SliceBuildError) as cm:
            ff._build_slices_live(c, known_total=37191)
        self.assertEqual(cm.exception.probe_errors, 0)

    def test_zero_slices_with_probe_errors_raises_slice_build_error(self):
        c = FakeClient(districts=[60], list_errors={(60, ""): 99})
        with self.assertRaises(ff.SliceBuildError) as cm:
            ff._build_slices_live(c, known_total=100)
        self.assertGreater(cm.exception.probe_errors, 0)

    def test_zero_slices_small_city_without_errors_is_done(self):
        c = FakeClient(districts=[60])
        self.assertEqual(ff._build_slices_live(c, known_total=100), [])

    def test_crawl_slice_build_error_keeps_city_and_page(self):
        st = multi_state(["vung-tau", "lam-dong"], "vung-tau", page=167, total=37191,
                         done={"1001": "x"})
        c = FakeClient()
        err = ff.SliceBuildError("vung-tau", 37191, 4)

        def build(_c, **kw):
            self.assertEqual(kw.get("known_total"), 37191)
            raise err

        def sleep(s):
            self.sleeps.append(s)
            self.stop()

        with mock.patch.object(ff, "list_page", lambda c, page, **kw: ([], 37191)), \
                mock.patch.object(ff, "_build_slices_live", build), mock.patch.object(ff.time, "sleep", sleep):
            ff._crawl(crawl_args(), st, self.stp, None, c, 167)
        self.assertEqual(st["city"], "vung-tau")
        bucket = st["cities"]["vung-tau"]
        self.assertEqual((bucket["page"], bucket.get("slice", 0)), (167, 0))
        self.assertNotIn("slices", bucket)
        self.assertEqual(bucket["slice_build_error"]["probe_errors"], 4)
        self.assertEqual(self.sleeps, [ff.SLICE_RETRY_PAUSE])
        self.assertEqual(len(self.hb_errors("foody_slice_build")), 1)
        with open(self.stp, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["city"], saved["cities"]["vung-tau"]["page"]), ("vung-tau", 167))

    def test_da_lat_skipped_and_lam_dong_kept(self):
        self.assertIn("da-lat", fc.SKIP_SLUGS)
        slugs = {c["slug"] for c in fc.seed_cities()}
        self.assertNotIn("da-lat", slugs)
        self.assertIn("lam-dong", slugs)
        fc.save_cached(self.root, [{"id": 281, "slug": "da-lat", "name": "Đà Lạt"},
                                   {"id": 254, "slug": "lam-dong", "name": "Lâm Đồng"}])
        self.assertEqual([c["slug"] for c in fc.load_cached(self.root)], ["lam-dong"])

    def test_cache_with_only_skipped_slugs_is_ignored(self):
        fc.save_cached(self.root, [{"id": 281, "slug": "da-lat", "name": "Đà Lạt"}])
        self.assertIsNone(fc.load_cached(self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
