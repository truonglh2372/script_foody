"""Offline tests for common/atomic_io.py and the state writers that use it (foody crash 12/11 12:39:25:
os.replace onto a _state.json that Windows AV / the indexer / a reader had open -> PermissionError, Access is denied).

Run: python3 tests/test_atomic_io.py
"""
import json, os, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "common"))
import atomic_io  # noqa: E402


def sharing_violation():
    e = OSError(13, "The process cannot access the file because it is being used by another process")
    e.winerror = 32
    return e


class FlakyReplace:
    """os.replace that raises `errors` first (one per call), then does the real replace."""

    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    def __call__(self, src, dst):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        os.replace(src, dst)


class ReplaceRetryTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.d, "s.json.tmp"), os.path.join(self.d, "s.json")
        open(self.src, "w").write('{"new": 1}'); open(self.dst, "w").write('{"old": 1}')
        self.sleeps, self.logs = [], []

    def retry(self, rep, **kw):
        return atomic_io.replace_retry(self.src, self.dst, sleep=self.sleeps.append, replace=rep, log=self.logs.append, **kw)

    def test_succeeds_after_transient_permission_errors(self):
        rep = FlakyReplace([PermissionError(13, "Access is denied"), sharing_violation(), PermissionError(13, "x")])
        self.assertEqual(self.retry(rep), 4)
        self.assertEqual(json.load(open(self.dst)), {"new": 1})
        self.assertFalse(os.path.exists(self.src))
        self.assertEqual(self.sleeps, [0.05, 0.1, 0.2])                  # exponential backoff
        self.assertIn("sau 4 lần thử", self.logs[-1])

    def test_gives_up_after_max_attempts_keeps_tmp_and_old_target(self):
        rep = FlakyReplace([PermissionError(13, "Access is denied")] * 20)
        with self.assertRaises(PermissionError):
            self.retry(rep)
        self.assertEqual(rep.calls, 8)                                   # ATTEMPTS
        self.assertEqual(self.sleeps, [0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0])  # capped at 2 s, ~5.15 s total
        self.assertEqual(json.load(open(self.src)), {"new": 1})          # temp file kept (new content not lost)
        self.assertEqual(json.load(open(self.dst)), {"old": 1})          # target intact
        self.assertIn("vẫn bị khoá sau 8 lần thử", self.logs[-1]); self.assertIn(self.src, self.logs[-1])

    def test_non_permission_errors_are_not_retried(self):
        for err in (FileNotFoundError(2, "No such file"), OSError(28, "No space left on device"), IsADirectoryError(21, "dir")):
            rep = FlakyReplace([err])
            with self.assertRaises(type(err)):
                self.retry(rep)
            self.assertEqual(rep.calls, 1, err)
        self.assertEqual(self.sleeps, [])

    def test_locked_error_classification(self):
        self.assertTrue(atomic_io.is_locked_error(PermissionError(13, "x")))
        self.assertTrue(atomic_io.is_locked_error(sharing_violation()))
        lock = OSError(13, "lock"); lock.winerror = 33
        self.assertTrue(atomic_io.is_locked_error(lock))
        other = OSError(5, "io"); other.winerror = 1117
        self.assertFalse(atomic_io.is_locked_error(other))
        self.assertFalse(atomic_io.is_locked_error(FileNotFoundError(2, "x")))
        self.assertEqual([atomic_io.backoff(n) for n in range(8)], [0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0])

    def test_write_json_atomic_closes_tmp_before_replace(self):
        seen = []

        def rep(src, dst):
            with open(src, encoding="utf-8") as f:   # fully flushed + closed
                seen.append(json.load(f))
            os.replace(src, dst)
        atomic_io.write_json_atomic(self.dst, {"a": "ổ"}, ensure_ascii=False, replace_kw={"replace": rep})
        self.assertEqual(seen, [{"a": "ổ"}]); self.assertEqual(json.load(open(self.dst, encoding="utf-8")), {"a": "ổ"})


class SaveStateTest(unittest.TestCase):
    """foody_fetch._save_state: a locked _state.json no longer crashes the crawl; the old file is never moved away."""

    def setUp(self):
        import foody_fetch as ff
        self.ff = ff
        self.d = tempfile.mkdtemp(); self.stp = os.path.join(self.d, "_state.json")
        json.dump({"page": 3, "done": {"1": 1}}, open(self.stp, "w"))
        ff._SAVE_FAILS["n"] = 0
        self.addCleanup(lambda: ff._SAVE_FAILS.update(n=0))
        p = mock.patch.object(atomic_io.time, "sleep", lambda s: None); p.start(); self.addCleanup(p.stop)

    def locked(self, n):
        errs = [PermissionError(13, "Access is denied")] * n
        real = os.replace

        def rep(src, dst):
            if errs and dst == self.stp:
                errs.pop(); raise PermissionError(13, "Access is denied")
            return real(src, dst)
        return mock.patch.object(atomic_io.os, "replace", rep)

    def test_transient_lock_is_retried(self):
        st = {"page": 4, "done": {"1": 1, "2": 1}}
        with self.locked(3):
            self.assertTrue(self.ff._save_state(st, self.stp))
        self.assertEqual(json.load(open(self.stp))["page"], 4)
        self.assertEqual(json.load(open(self.stp + ".bak"))["page"], 3)

    def test_persistent_lock_keeps_running_then_raises(self):
        st = {"page": 5, "done": {}}
        with self.locked(10 ** 6):
            self.assertFalse(self.ff._save_state(st, self.stp))
            self.assertFalse(self.ff._save_state(st, self.stp))
            self.assertEqual(json.load(open(self.stp))["page"], 3)       # last good state still in place, not moved
            self.assertEqual(json.load(open(self.stp + ".tmp"))["page"], 5)  # new state kept in the temp file
            with self.assertRaises(PermissionError):
                self.ff._save_state(st, self.stp)                          # 3rd save in a row -> give up loudly
        self.assertTrue(self.ff._save_state(st, self.stp))                 # lock gone -> saved, counter reset
        self.assertEqual((json.load(open(self.stp))["page"], self.ff._SAVE_FAILS["n"]), (5, 0))

    def test_other_errors_still_raise_at_once(self):
        with mock.patch.object(atomic_io.os, "replace", mock.Mock(side_effect=OSError(28, "No space left"))) as m:
            with self.assertRaises(OSError):
                self.ff._save_state({"page": 6}, self.stp)
            self.assertEqual(m.call_count, 1)

    def test_load_state_falls_back_to_bak_when_missing(self):
        os.replace(self.stp, self.stp + ".bak")
        self.assertEqual(self.ff._load_state(self.stp)["page"], 3)
        os.remove(self.stp + ".bak")
        self.assertEqual(self.ff._load_state(self.stp), {"page": 1, "done": {}, "failed": {}})


class EveryStateWriterUsesRetryTest(unittest.TestCase):
    """No state writer calls a bare os.replace any more (only atomic_io itself and the shared-file fallback)."""

    def test_no_bare_os_replace(self):
        allowed = {os.path.join("common", "atomic_io.py"): None,
                   os.path.join("common", "slack_notify.py"): 1, os.path.join("common", "disk_cleanup.py"): 1}
        for sub in ("common", "scripts"):
            for name in os.listdir(os.path.join(REPO, sub)):
                if not name.endswith(".py"):
                    continue
                rel = os.path.join(sub, name)
                code = [l for l in open(os.path.join(REPO, rel), encoding="utf-8").read().splitlines()
                        if "os.replace(" in l and not l.strip().startswith(("#", '"""')) and "os.replace with" not in l
                        and "os.replace(_state" not in l and "os.replace(src, dst) with" not in l]
                if rel in allowed:
                    if allowed[rel] is not None:
                        self.assertEqual(len(code), allowed[rel], (rel, code))   # the ImportError fallback only
                    continue
                self.assertEqual(code, [], rel)

    def test_shared_files_use_retry(self):
        import slack_notify, disk_cleanup
        for mod in (slack_notify, disk_cleanup):
            calls = []
            with mock.patch.object(atomic_io, "replace_retry", lambda s, d: calls.append((s, d))):
                mod._replace("a", "b")
            self.assertEqual(calls, [("a", "b")], mod.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=1)
