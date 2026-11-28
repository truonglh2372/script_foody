"""Server-driven pause (script_foody#32, contract an_uong_quanh_day#1651): a write answered 503 {"error":"ingest_paused"}
sleeps Retry-After (default 300, cap 900) and retries the SAME request; it is never an error and never dropped.
No network: the HTTP session, the tokens and the sleep are fakes.

Run: python3 tests/test_ingest_pause.py
"""
import io, json, os, sys, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
os.environ.pop("AQD_ORIGIN_IP", None)
import ingest_pause  # noqa: E402
import aqd_api  # noqa: E402


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.headers, self._body = status, headers or {}, body
        self.reason = "x"

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def paused(retry_after="300", body=None):
    return FakeResponse(503, body or {"error": "ingest_paused", "retry_after": 300},
                        {"Retry-After": retry_after} if retry_after is not None else {})


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


class PauseTestBase(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.clock = [1000.0]
        ingest_pause.STATE.reset()
        self.addCleanup(ingest_pause.STATE.reset)
        for target, value in (("_sleep", lambda s: (self.sleeps.append(s), self.clock.__setitem__(0, self.clock[0] + s))),
                              ("_now", lambda: self.clock[0])):
            p = mock.patch.object(ingest_pause, target, value); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(aqd_api.time, "sleep", lambda s: self.sleeps.append(("backoff", s))); p.start(); self.addCleanup(p.stop)
        self.client = aqd_api.AQD(base="https://aqd.test")
        self.client.tokens.access = lambda force=False: "tok"

    def use(self, *responses):
        self.session = FakeSession(*responses)
        self.client._tl.s = self.session
        return self.session


class RetryAfterTest(unittest.TestCase):
    def test_default_cap_and_junk(self):
        f = ingest_pause.retry_after_seconds
        self.assertEqual(300, f(None)); self.assertEqual(300, f("")); self.assertEqual(300, f("soon"))
        self.assertEqual(120, f("120")); self.assertEqual(900, f("5000")); self.assertEqual(900, f(900))
        self.assertEqual(1, f("0")); self.assertEqual(300, f("-5"))


class CallPauseTest(PauseTestBase):
    def test_paused_write_sleeps_retry_after_then_retries_the_same_request(self):
        s = self.use(paused("120"), FakeResponse(200, {"ok": 1}))
        self.assertEqual({"ok": 1}, self.client.call("POST", "/v1/admin/restaurants/import", json_body={"a": 1}, retries=0))
        self.assertEqual([120], self.sleeps)
        self.assertEqual(2, len(s.calls))
        self.assertEqual(s.calls[0][:2], s.calls[1][:2]); self.assertEqual({"a": 1}, s.calls[1][2]["json"])

    def test_default_300_and_cap_900(self):
        self.use(paused(None), paused("99999"), FakeResponse(200, {}))
        self.client.call("POST", "/v1/admin/x", json_body={}, retries=0)
        self.assertEqual([300, 900], self.sleeps)

    def test_body_retry_after_is_used_when_the_header_is_missing(self):
        self.use(paused(None, {"error": "ingest_paused", "retry_after": 45}), FakeResponse(200, {}))
        self.client.call("POST", "/v1/admin/x", json_body={}, retries=0)
        self.assertEqual([45], self.sleeps)

    def test_pause_never_consumes_the_retry_budget(self):
        self.use(*[paused("10")] * 6, FakeResponse(200, {"ok": 1}))
        self.assertEqual({"ok": 1}, self.client.call("POST", "/v1/admin/x", json_body={}, retries=1))
        self.assertEqual([10] * 6, self.sleeps)

    def test_photo_post_paths_without_5xx_retry_still_pause(self):
        # _server_batch / multipart uploads use retry_status=(429,), retry_network=False
        s = self.use(paused("7"), FakeResponse(201, {"photos": [], "errors": []}))
        files = {"image": ("a.jpg", io.BytesIO(b"x"), "image/jpeg")}
        self.client.call("POST", "/v1/admin/restaurants/5/photos", files=files, retries=3, retry_status=(429,), retry_network=False)
        self.assertEqual([7], self.sleeps); self.assertEqual(2, len(s.calls))

    def test_other_503_is_unchanged(self):
        self.use(FakeResponse(503, {"error": "boom"}))
        with self.assertRaises(aqd_api.APIError) as raised:
            self.client.call("POST", "/v1/admin/x", json_body={}, retries=0)
        self.assertEqual(503, raised.exception.status); self.assertEqual([], self.sleeps)

    def test_other_503_still_backs_off_and_retries(self):
        self.use(FakeResponse(503, {"error": "boom"}, {"Retry-After": "3"}), FakeResponse(200, {"ok": 1}))
        self.assertEqual({"ok": 1}, self.client.call("POST", "/v1/admin/x", json_body={}, retries=2))
        self.assertEqual([("backoff", 3.0)], self.sleeps)

    def test_enter_and_leave_are_logged_once_not_per_request(self):
        self.use(paused("5"), paused("5"), paused("5"), FakeResponse(200, {}))
        with self.assertLogs("ingest_pause", "WARNING") as logs:
            self.client.call("POST", "/v1/admin/x", json_body={}, retries=0)
        self.assertEqual(1, sum("paused" in m and "resum" not in m for m in logs.output))
        self.assertEqual(1, sum("resum" in m for m in logs.output))

    def test_a_pause_learned_elsewhere_delays_the_next_write(self):
        ingest_pause.STATE.enter(60, "heartbeat")
        s = self.use(FakeResponse(200, {"ok": 1}))
        self.client.call("POST", "/v1/admin/x", json_body={}, retries=0)
        self.assertEqual([60], self.sleeps); self.assertEqual(1, len(s.calls))

    def test_reads_are_not_delayed(self):
        ingest_pause.STATE.enter(60, "heartbeat")
        self.use(FakeResponse(200, {"ok": 1}))
        self.client.call("GET", "/v1/admin/restaurants/1", retries=0)
        self.assertEqual([], self.sleeps)


if __name__ == "__main__":
    unittest.main()
