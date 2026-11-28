#!/usr/bin/env python3
"""Server-driven pause (script_foody#32; contract an_uong_quanh_day#1651).

While the server's `ingest_paused` switch is on, every write to /v1/admin/* and /v1/ingest/* answers
`503`, header `Retry-After: 300`, body {"error":"ingest_paused","retry_after":300}; the heartbeat answer carries
"paused": true. The crawler then sleeps Retry-After seconds (default 300, cap 900) and retries the SAME request: the
item is never failed, never counted as an error, never dropped. Entering and leaving the pause is logged once.
The local STOP / STOP_ALL files keep working as the fallback.
"""
import logging, threading, time

DEFAULT_RETRY_AFTER = 300
MAX_RETRY_AFTER = 900
ERROR_CODE = "ingest_paused"
_sleep = time.sleep
_now = time.monotonic
_log = logging.getLogger("ingest_pause")


def retry_after_seconds(value):
    """Seconds to wait from a Retry-After header / retry_after field: default 300 when missing or junk, 1..900."""
    try:
        seconds = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
    if seconds < 0:
        return DEFAULT_RETRY_AFTER
    return max(1, min(seconds, MAX_RETRY_AFTER))


def is_pause_response(status, body):
    """True for a 503 whose JSON error is ingest_paused (error may be a string or {"code": ...})."""
    if status != 503 or not isinstance(body, dict):
        return False
    err = body.get("error")
    code = err.get("code") if isinstance(err, dict) else err
    return code == ERROR_CODE


class PauseState:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        self.active = False
        self.until = 0.0

    def enter(self, seconds, source="server"):
        """Pause writes for `seconds` more (extends, never shortens). Logs only when the pause starts."""
        seconds = retry_after_seconds(seconds)
        with self._lock:
            started = not self.active
            self.active = True
            self.until = max(self.until, _now() + seconds)
        if started:
            _log.warning("ingest paused by the server (%s): writes wait %ss and are retried, nothing is dropped", source, seconds)

    def remaining(self):
        return max(0.0, self.until - _now()) if self.active else 0.0

    def wait(self):
        """Sleep out the pause. The pause stays "entered" until leave() (the first answer that is not a pause), so
        repeated 503s do not log again. No-op when not paused."""
        left = self.remaining()
        while left > 0:
            _sleep(left)
            left = self.remaining()

    def leave(self):
        with self._lock:
            was = self.active
            self.active = False
            self.until = 0.0
        if was:
            _log.warning("ingest resumed: the server accepts writes again")


STATE = PauseState()
