"""Atomic file replace that survives a briefly locked target on Windows (foody crash 12/11 12:39:25:
os.replace(_state.json.tmp, _state.json) -> PermissionError [WinError 5] Access is denied, while antivirus / the
search indexer / a reader had _state.json open).

    from atomic_io import replace_retry, write_json_atomic
    write_json_atomic(path, obj)           # write <path>.tmp, then replace_retry(<path>.tmp, path)

replace_retry retries only "file is locked" errors: PermissionError, or an OSError whose winerror is
5 (ACCESS_DENIED), 32 (SHARING_VIOLATION) or 33 (LOCK_VIOLATION). Up to ATTEMPTS (8) tries, sleeping
BASE * 2**n seconds between them, capped at CAP (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0 -> about 5 s in total).
When it still fails it logs one line and RE-RAISES the last error; the temp file is left in place (never deleted
here), so the new content is still on disk next to the old, intact target. Any other error (disk full, missing
folder, ...) is raised at once, without retrying. Callers decide what a final failure means (see
foody_fetch._save_state: keep running with the state in memory, give up after a few saves in a row).
Stdlib only.
"""
import json, os, time

ATTEMPTS = 8
BASE = 0.05
CAP = 2.0
LOCKED_WINERRORS = (5, 32, 33)


def is_locked_error(e):
    """True for 'someone has the file open' errors that are worth retrying."""
    if isinstance(e, PermissionError):
        return True
    return isinstance(e, OSError) and getattr(e, "winerror", None) in LOCKED_WINERRORS


def backoff(n, base=BASE, cap=CAP):
    """Delay before retry n (0-based)."""
    return min(cap, base * (2 ** n))


def replace_retry(src, dst, attempts=ATTEMPTS, base=BASE, cap=CAP, sleep=None, replace=None, log=None):
    """os.replace(src, dst) with retries on a locked target (see module doc). Returns the number of tries used."""
    attempts = max(1, int(attempts))
    sleep = sleep or time.sleep
    replace = replace or os.replace
    for n in range(attempts):
        try:
            replace(src, dst)
            if n and log:
                log(f"[atomic_io] {os.path.basename(dst)}: ghi được sau {n + 1} lần thử (file bị khoá tạm thời)")
            return n + 1
        except OSError as e:
            if not is_locked_error(e):
                raise
            if n == attempts - 1:
                (log or _log)(f"[atomic_io] {os.path.basename(dst)}: vẫn bị khoá sau {attempts} lần thử "
                              f"({type(e).__name__}: {e}); giữ file tạm {src}")
                raise
            sleep(backoff(n, base, cap))
    raise AssertionError("unreachable")


def write_json_atomic(path, obj, tmp=None, encoding="utf-8", replace_kw=None, **dump_kw):
    """json.dump to <path>.tmp (closed before the replace - an open handle would block it on Windows), then
    replace_retry onto path."""
    tmp = tmp or (str(path) + ".tmp")
    with open(tmp, "w", encoding=encoding) as f:
        json.dump(obj, f, **dump_kw)
    replace_retry(tmp, path, **(replace_kw or {}))
    return path


def _log(msg):
    print(msg, flush=True)
