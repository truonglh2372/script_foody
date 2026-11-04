"""ConnectTimeout on a Foody list page (Admin crash 04/11 12:18, page 52 district 6).

After AQD_FOODY_LIST_TIMEOUT_SKIP ConnectTimeouts in a row the page is skipped, logged and reported to Slack instead
of crashing the crawler; waits between timeouts follow AQD_FOODY_TIMEOUT_BACKOFF (default 1, 2, 5, 10 min).
No network: requests and time.sleep are mocked.
Run: python3 tests/test_list_timeout_skip.py
"""
import importlib, os, sys, tempfile, types, unittest
from unittest import mock

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
os.environ.setdefault("AQD_FOODY_DATA", tempfile.mkdtemp(prefix="foody_test_"))
import foody_fetch as ff  # noqa: E402


def ok_response(payload):
    r = mock.Mock(status_code=200, headers={"content-type": "application/json"}, text="{}")
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class ClientBackoffTest(unittest.TestCase):
    def client(self, side_effect):
        c = ff.Client()
        c.warm = True
        c.s = mock.Mock()
        c.s.get.side_effect = side_effect
        return c

    def test_gives_up_with_skip_signal_after_n_timeouts(self):
        c = self.client([requests.ConnectTimeout("t")] * 20)
        with mock.patch.object(ff.time, "sleep") as sl, mock.patch.object(ff, "DELAY", 0):
            with self.assertRaises(ff.ConnectTimeoutGiveUp) as cm:
                c.get("/__get/Place/HomeListPlace?page=52", xhr=True)
        self.assertEqual(cm.exception.count, ff.LIST_TIMEOUT_SKIP)
        self.assertEqual(c.s.get.call_count, 6)
        waits = [x.args[0] for x in sl.call_args_list if x.args and x.args[0] >= 1]
        self.assertEqual(waits, [60, 120, 300, 600, 600])

    def test_recovers_after_some_timeouts(self):
        c = self.client([requests.ConnectTimeout("t")] * 3 + [ok_response({"Items": [{"Id": 1}], "Total": 9})])
        with mock.patch.object(ff.time, "sleep"), mock.patch.object(ff, "DELAY", 0):
            self.assertEqual(ff.list_page(c, 52), ([{"Id": 1}], 9))

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"AQD_FOODY_LIST_TIMEOUT_SKIP": "3", "AQD_FOODY_TIMEOUT_BACKOFF": "5, 7"}):
            m = importlib.reload(ff)
            try:
                self.assertEqual(m.LIST_TIMEOUT_SKIP, 3)
                self.assertEqual([m.timeout_wait(i) for i in (1, 2, 3, 4)], [5, 7, 7, 7])
            finally:
                os.environ.pop("AQD_FOODY_LIST_TIMEOUT_SKIP"); os.environ.pop("AQD_FOODY_TIMEOUT_BACKOFF")
                importlib.reload(ff)

    def test_bad_env_falls_back_to_defaults(self):
        with mock.patch.dict(os.environ, {"AQD_FOODY_LIST_TIMEOUT_SKIP": "x", "AQD_FOODY_TIMEOUT_BACKOFF": "a,b"}):
            m = importlib.reload(ff)
            try:
                self.assertEqual(m.LIST_TIMEOUT_SKIP, 6)
                self.assertEqual(m.TIMEOUT_BACKOFF, [60, 120, 300, 600])
            finally:
                os.environ.pop("AQD_FOODY_LIST_TIMEOUT_SKIP"); os.environ.pop("AQD_FOODY_TIMEOUT_BACKOFF")
                importlib.reload(ff)


class CrawlSkipsTimeoutPageTest(unittest.TestCase):
    def test_crawl_skips_page_logs_and_notifies(self):
        global ff
        ff = importlib.reload(ff)
        root = tempfile.mkdtemp(prefix="foody_crawl_")
        stp = os.path.join(root, "_state.json")
        st = {"page": 52, "done": {}, "failed": {}}
        seen = []

        def fake_list(c, page, **kw):
            seen.append(page)
            if page == 52:
                raise ff.ConnectTimeoutGiveUp("https://www.foody.vn/__get/Place/HomeListPlace?page=52", 6)
            open(os.path.join(root, "STOP"), "w").close()  # stop after the next page
            return [], None

        hb = mock.Mock()
        a = types.SimpleNamespace(limit=0, parity="all", refetch=False, min_free_gb=0)
        with mock.patch.object(ff, "ROOT", root), mock.patch.object(ff, "list_page", fake_list), \
                mock.patch.object(ff, "HB", hb), mock.patch.object(ff, "plan_empty_page", return_value=("skip", 54, 0)), \
                mock.patch.object(ff.time, "sleep"), mock.patch("builtins.print") as pr:
            ff._crawl(a, st, stp, None, object(), 52)
        self.assertEqual(seen[:2], [52, 53])
        self.assertEqual(st["timeout_skipped"][0]["page"], 52)
        out = " ".join(str(x.args[0]) for x in pr.call_args_list if x.args)
        self.assertIn("trang 52 ConnectTimeout 6 lần liên tiếp -> bỏ qua, sang trang 53", out)
        errs = [x for x in hb.error.call_args_list if x.args[0] == "foody_list_timeout"]
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].kwargs.get("severe"))
        self.assertNotIn("webhook", str(errs[0]).lower())


if __name__ == "__main__":
    unittest.main()
