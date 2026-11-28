"""Heartbeats to /v1/ingest/heartbeats (script_foody#32): at least every 5 minutes, machine host name + crawler version,
a `paused` answer puts the crawler into pause, failures never stop crawling. No network: fake opener, fake clock.

Run: python3 tests/test_heartbeat.py
"""
import io, json, os, sys, unittest, urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
os.environ.pop("AQD_ORIGIN_IP", None)
import heartbeat  # noqa: E402
import ingest_pause  # noqa: E402


class FakeResponse:
    def __init__(self, status=201, body=None):
        self.status, self._raw = status, json.dumps(body if body is not None else {}).encode()

    def getcode(self):
        return self.status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes) or [FakeResponse()], []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def bodies(self):
        return [json.loads(r.data) for r in self.requests]


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        ingest_pause.STATE.reset()
        self.addCleanup(ingest_pause.STATE.reset)
        self.t = [1000.0]
        self.stats = {"processed": 0, "photos": 0, "errors": 0}
        self.dir = os.environ.get("TMP") or "."

    def hb(self, *outcomes, **kw):
        self.opener = FakeOpener(*outcomes)
        kw.setdefault("hostname", "mac-mini")
        return heartbeat.Heartbeat("script_foody", base="https://aqd.test", token="secret-token", opener=self.opener,
                                   now=lambda: self.t[0], version="abc1234", counters=lambda: dict(self.stats),
                                   disk_dir=self.dir, **kw)

    def test_no_token_means_no_requests(self):
        h = heartbeat.Heartbeat("script_foody", base="https://aqd.test", token="", opener=FakeOpener(), now=lambda: 0)
        self.assertFalse(h.enabled); self.assertFalse(h.beat(force=True)); self.assertEqual([], h.opener.requests)

    def test_body_has_host_status_counters_and_version(self):
        h = self.hb(); self.stats.update(processed=3, photos=9, errors=1); self.t[0] += 60
        self.assertTrue(h.beat(force=True))
        req = self.opener.requests[0]
        self.assertEqual("https://aqd.test/v1/ingest/heartbeats", req.full_url)
        self.assertEqual("Bearer secret-token", req.get_header("Authorization"))
        self.assertTrue(req.get_header("Idempotency-key"))
        b = self.opener.bodies()[0]
        self.assertEqual(("script_foody", "mac-mini", "running"), (b["crawler"], b["host"], b["status"]))
        self.assertEqual((3, 9, 1, 60), (b["processed_count"], b["photo_count"], b["error_count"], b["window_seconds"]))
        self.assertIn("abc1234", b["note"])

    def test_counters_are_deltas_since_the_last_accepted_heartbeat(self):
        h = self.hb(); self.stats["processed"] = 5; h.beat(force=True)
        self.stats["processed"] = 7; self.t[0] += 300; h.beat()
        self.assertEqual([5, 2], [b["processed_count"] for b in self.opener.bodies()])

    def test_cadence_at_least_every_five_minutes(self):
        h = self.hb()
        self.assertTrue(h.tick())              # first one right away
        self.t[0] += 299; self.assertFalse(h.tick())
        self.t[0] += 2;   self.assertTrue(h.tick())
        self.assertEqual(2, len(self.opener.requests))

    def test_paused_answer_puts_the_crawler_into_pause(self):
        h = self.hb(FakeResponse(201, {"paused": True, "retry_after": 120}))
        h.beat(force=True)
        self.assertTrue(ingest_pause.STATE.active)
        self.assertTrue(118 <= ingest_pause.STATE.remaining() <= 120)

    def test_not_paused_answer_resumes(self):
        ingest_pause.STATE.enter(300, "test")
        h = self.hb(FakeResponse(201, {"paused": False}))
        h.beat(force=True)
        self.assertFalse(ingest_pause.STATE.active)

    def test_status_is_blocked_with_a_note_while_paused_and_a_beat_is_sent_on_the_change(self):
        h = self.hb(FakeResponse(201, {"paused": True, "retry_after": 300}), FakeResponse(201, {"paused": False}))
        h.tick()                                # learns pause
        self.t[0] += 10; h.tick()               # state changed since the last beat: sent early, not after 5 min
        self.assertEqual("blocked", self.opener.bodies()[1]["status"])
        self.assertIn("ingest_paused", self.opener.bodies()[1]["note"])
        self.assertFalse(ingest_pause.STATE.active)
        self.t[0] += 10; h.tick()               # resumed: another early beat reports it
        self.assertEqual("running", self.opener.bodies()[2]["status"])

    def test_failures_never_raise_and_are_retried_with_the_same_body_and_key(self):
        h = self.hb(urllib.error.URLError("down"), urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b"{}")), FakeResponse(201, {}))
        self.stats["processed"] = 4
        self.assertFalse(h.beat(force=True)); self.assertFalse(h.beat(force=True)); self.assertTrue(h.beat(force=True))
        keys = {r.get_header("Idempotency-key") for r in self.opener.requests}
        self.assertEqual(1, len(keys)); self.assertEqual([4, 4, 4], [b["processed_count"] for b in self.opener.bodies()])

    def test_rejected_body_is_dropped(self):
        h = self.hb(urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(b"{}")), FakeResponse(201, {}))
        h.beat(force=True); h.beat(force=True)
        keys = [r.get_header("Idempotency-key") for r in self.opener.requests]
        self.assertNotEqual(keys[0], keys[1])

    def test_503_ingest_paused_on_the_heartbeat_itself_enters_pause_without_crashing(self):
        err = urllib.error.HTTPError("u", 503, "x", {"Retry-After": "60"}, io.BytesIO(b'{"error":"ingest_paused"}'))
        h = self.hb(err)
        self.assertFalse(h.beat(force=True)); self.assertTrue(ingest_pause.STATE.active)

    def test_start_runs_a_daemon_thread_that_ticks(self):
        h = self.hb()
        with mock.patch.object(h, "tick") as tick:
            h.start(poll=0.01)
            for _ in range(200):
                if tick.called:
                    break
                h._stop.wait(0.01)
            h.stop()
        self.assertTrue(tick.called)



class WiringTest(unittest.TestCase):
    def test_foody_fetch_builds_the_heartbeat_from_pusher_stats(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
        import foody_fetch
        pusher = mock.Mock(stats={"pushed": 4, "photos": 11, "fail": 2, "reviews": 0})
        with mock.patch.dict(os.environ, {"AQD_INGEST_TOKEN": "t"}):
            h = foody_fetch._make_server_heartbeat(pusher, start=False)
        self.assertTrue(h.enabled)
        self.assertEqual({"processed": 4, "photos": 11, "errors": 2}, h.counters())
        self.assertEqual("script_foody", h.crawler)

    def test_without_a_token_the_wiring_is_a_disabled_noop(self):
        import foody_fetch
        with mock.patch.dict(os.environ, {"AQD_INGEST_TOKEN": ""}):
            self.assertFalse(foody_fetch._make_server_heartbeat(None, start=False).enabled)


if __name__ == "__main__":
    unittest.main()
