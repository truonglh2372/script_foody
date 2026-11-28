#!/usr/bin/env python3
"""Crawler heartbeat to the server (script_foody#32; web contract an_uong_quanh_day#1021 / #1651).

    POST {AQD_BASE}/v1/ingest/heartbeats
    Authorization: Bearer $AQD_INGEST_TOKEN     (Admin -> Ingest clients; one token per machine; never logged)
    Idempotency-Key: <fresh per heartbeat>      (a retry resends the same body with the same key)
    {"crawler": "script_foody", "host": "mac-mini", "status": "running|blocked|idle|error", "window_seconds": 300,
     "processed_count": 12, "photo_count": 30, "dish_count": 0, "error_count": 1, "rate_limited_count": 0,
     "disk_free_mb": 51200, "note": "v<git version>; ..."}

- A background thread sends one at least every INTERVAL_S (5 min) while the crawler runs, and one right away when the
  pause state changes. The answer's "paused": true puts the crawler into the server-driven pause (ingest_pause.py),
  "paused": false ends it. status is "blocked" with note "ingest_paused" while paused.
- Counters are deltas since the last accepted heartbeat (the tally comes from the `counters` callable).
- No token (AQD_INGEST_TOKEN unset) or AQD_HEARTBEAT=0 -> no-op. Every failure is swallowed: a heartbeat never stops
  crawling. 409/422 drop that body; a 503 ingest_paused on the heartbeat itself also enters the pause.
- Uses urllib, so the AQD_ORIGIN_IP pin applies and TLS is verified against the AQD_BASE host name.
"""
import json, logging, os, shutil, socket, threading, time, urllib.error, urllib.request, uuid

import ingest_pause
import origin_pin

PATH = "/v1/ingest/heartbeats"
INTERVAL_S = 300
RETRY_S = 60
POLL_S = 15
COUNT_FIELDS = {"processed": "processed_count", "photos": "photo_count", "dishes": "dish_count",
                "errors": "error_count", "rate_limited": "rate_limited_count"}
_log = logging.getLogger("heartbeat")


class Heartbeat:
    def __init__(self, crawler, base=None, token=None, opener=None, now=time.monotonic, version=None, hostname=None,
                 counters=None, disk_dir=".", interval=INTERVAL_S, pause=None):
        self.crawler = crawler
        self.base = (base or os.environ.get("AQD_BASE") or "https://anuongquanhday.com").rstrip("/")
        if token is None:
            token = os.environ.get("AQD_INGEST_TOKEN", "")
        self.token = token.strip()
        self.enabled = bool(self.token) and os.environ.get("AQD_HEARTBEAT", "1").strip() != "0"
        self.opener, self.now, self.version = opener or urllib.request.urlopen, now, version
        self.hostname = (hostname or os.environ.get("CRAWLER_HOSTNAME") or socket.gethostname() or "unknown")[:100]
        self.counters, self.disk_dir, self.interval = counters or (lambda: {}), disk_dir, interval
        self.pause = pause or ingest_pause.STATE
        self.sent_at = None
        self.window_start = now()
        self.last_try = None
        self.last_paused = False
        self.base_counts = {}
        self.pending = None
        self.last_error = None
        self._stop = threading.Event()
        self._thread = None
        if self.enabled:
            try:
                origin_pin.install(self.base)  # same pin as the API client; never fatal here
            except Exception as e:  # noqa: BLE001
                _log.warning("heartbeat: AQD_ORIGIN_IP ignored (%s)", type(e).__name__)

    # ---- body ------------------------------------------------------------------------------------------------------
    def _disk_free_mb(self):
        try:
            return int(shutil.disk_usage(self.disk_dir).free // (1024 * 1024))
        except OSError:
            return None

    def _body(self):
        now = self.now()
        totals = {k: int(v) for k, v in (self.counters() or {}).items() if isinstance(v, (int, float))}
        body = {"crawler": self.crawler, "host": self.hostname, "status": "blocked" if self.pause.active else "running",
                "window_seconds": int(max(0, now - self.window_start)) if self.window_start is not None else 0}
        for name, field in COUNT_FIELDS.items():
            body[field] = max(0, totals.get(name, 0) - self.base_counts.get(name, 0))
        note = [f"v{self.version}" if self.version else None, "ingest_paused" if self.pause.active else None]
        body["note"] = "; ".join(n for n in note if n) or None
        free = self._disk_free_mb()
        if free is not None:
            body["disk_free_mb"] = free
        return {k: v for k, v in body.items() if v is not None}, totals, self.pause.active

    # ---- send ------------------------------------------------------------------------------------------------------
    def beat(self, force=False):
        """Send now. True when the server stored it. Never raises."""
        if not self.enabled:
            return False
        try:
            return self._send()
        except Exception as e:  # noqa: BLE001 - a heartbeat must never break the crawler
            self.last_error = type(e).__name__
            return False

    def _send(self):
        self.last_try = self.now()
        if self.pending is None:
            body, totals, paused = self._body()
            self.pending = {"key": f"{self.crawler}-hb-{uuid.uuid4()}", "body": body, "totals": totals}
            self.last_paused = paused
        request = urllib.request.Request(
            self.base + PATH, data=json.dumps(self.pending["body"]).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self.token, "Idempotency-Key": self.pending["key"],
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener(request, timeout=15) as response:
                code = int(getattr(response, "status", None) or response.getcode())
                try:
                    answer = json.loads(response.read() or b"{}")
                except (ValueError, AttributeError):
                    answer = {}
        except urllib.error.HTTPError as e:
            self.last_error = f"http {e.code}"
            if e.code == 503:
                try:
                    answer = json.loads(e.read() or b"{}")
                except (ValueError, AttributeError):
                    answer = {}
                if ingest_pause.is_pause_response(503, answer):
                    self.pause.enter((e.headers or {}).get("Retry-After") or answer.get("retry_after"), "heartbeat")
            elif e.code in (409, 422):  # a body the server will never accept: drop it
                self.pending = None
            return False
        except Exception as e:  # noqa: BLE001
            self.last_error = type(e).__name__
            return False
        if not 200 <= code < 300:
            self.last_error = f"http {code}"
            return False
        self.base_counts = self.pending["totals"]
        self.window_start = self.sent_at = self.now()
        self.pending = None
        self.last_error = None
        if isinstance(answer, dict) and "paused" in answer:
            if answer.get("paused"):
                self.pause.enter(answer.get("retry_after"), "heartbeat")
            else:
                self.pause.leave()
        return True

    def due(self):
        now = self.now()
        if self.sent_at is None and self.last_try is None:
            return True
        if self.pause.active != self.last_paused:
            return self.last_try is None or now - self.last_try >= 1
        if self.pending is not None:  # a failed send: retry soon, same body and key
            return now - (self.last_try or 0) >= RETRY_S
        return now - (self.sent_at or 0) >= self.interval

    def tick(self):
        """Send if due (cadence, pause change or retry). Returns True when a heartbeat was stored."""
        return self.beat() if self.due() else False

    # ---- thread ----------------------------------------------------------------------------------------------------
    def start(self, poll=POLL_S):
        if not self.enabled or self._thread:
            return self

        def run():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    pass
                self._stop.wait(poll)
        self._thread = threading.Thread(target=run, name="aqd-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
