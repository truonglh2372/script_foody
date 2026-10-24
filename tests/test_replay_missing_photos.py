"""Tests for scripts/replay_missing_photos.py (re-fetch of Foody places imported with 0 photos, #1081).
Temp data dir only; no network. Run: python3 tests/test_replay_missing_photos.py"""
import io, json, os, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import replay_missing_photos as rp  # noqa: E402


def make_root():
    root = tempfile.mkdtemp(prefix="replay_")
    places = {
        "11": {"albums": {"3": 2, "2": 9, "1": 4}, "total_pictures": 15},   # candidate (menu+dish)
        "13": {"albums": {"1": 4, "4": 2}, "total_pictures": 6},            # venue/other only -> not a candidate
        "15": {"albums": {"2": 3}, "total_pictures": 3},                    # already has photos
        "17": {"albums": {"2": 3}, "total_pictures": 3},                    # never imported
        "19": {"total_pictures": 4},                                        # old record without album counts
    }
    for fid, p in places.items():
        os.makedirs(os.path.join(root, fid))
        p.update({"source_id": fid, "name": f"quan {fid}", "source_url": f"https://www.foody.vn/ho-chi-minh/q{fid}"})
        json.dump(p, open(os.path.join(root, fid, "place.json"), "w"))
    json.dump({"11": {"restaurant_id": 111, "photos": []}, "13": {"restaurant_id": 113, "photos": []},
               "15": {"restaurant_id": 115, "photos": ["x"]}, "19": {"restaurant_id": 119}},
              open(os.path.join(root, "_uploaded.json"), "w"))
    stp = os.path.join(root, "_state.json")
    json.dump({"page": 40, "done": {"11": "t", "19": "t"}, "failed": {}}, open(stp, "w"))
    old = time.time() - 3600
    os.utime(stp, (old, old))
    return root


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = rp.main(argv)
    return rc, buf.getvalue()


class Replay(unittest.TestCase):
    def setUp(self):
        self.root = make_root(); self.ids = os.path.join(self.root, "ids.tsv")
        self.stp = os.path.join(self.root, "_state.json")

    def test_list_picks_only_menu_or_dish_places(self):
        rc, out = run(["list", "--root", self.root, "--out", self.ids])
        self.assertEqual(rc, 0)
        self.assertEqual(rp.read_ids(self.ids), ["11", "19"])
        self.assertIn('"only_venue_or_other_albums": 1', out)

    def test_dry_run_writes_nothing(self):
        run(["list", "--root", self.root, "--out", self.ids])
        before = open(self.stp).read()
        rc, out = run(["enqueue", "--root", self.root, "--ids-file", self.ids])
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("max 5 per place", out)
        self.assertEqual(open(self.stp).read(), before)

    def test_apply_refused_while_crawler_runs(self):
        run(["list", "--root", self.root, "--out", self.ids])
        with mock.patch.object(rp, "crawler_running", return_value=True):
            rc, out = run(["enqueue", "--root", self.root, "--ids-file", self.ids, "--apply"])
        self.assertEqual(rc, 3)
        self.assertNotIn("gallery_retry", json.load(open(self.stp)))

    def test_apply_refused_when_state_fresh(self):
        run(["list", "--root", self.root, "--out", self.ids])
        os.utime(self.stp, None)
        with mock.patch.object(rp, "crawler_running", return_value=False):
            rc, _ = run(["enqueue", "--root", self.root, "--ids-file", self.ids, "--apply"])
        self.assertEqual(rc, 3)

    def test_apply_queues_for_gallery_retry_with_backup(self):
        run(["list", "--root", self.root, "--out", self.ids])
        with mock.patch.object(rp, "crawler_running", return_value=False):
            rc, out = run(["enqueue", "--root", self.root, "--ids-file", self.ids, "--apply", "--limit", "1"])
        self.assertEqual(rc, 0)
        st = json.load(open(self.stp))
        self.assertEqual(list(st["gallery_retry"]), ["11"])
        e = st["gallery_retry"]["11"]
        self.assertEqual((e["next_at"], e["tries"], e["item"]["Url"], e["item"]["Id"]), (0, 0, "/ho-chi-minh/q11", 11))
        self.assertEqual(st["page"], 40)
        self.assertTrue(any(f.startswith("_state.json.pre-replay-") for f in os.listdir(self.root)))

    def test_queued_item_is_due_for_the_crawler(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
        try:
            import foody_fetch as ff
        except ImportError:
            self.skipTest("requests not installed")
        run(["list", "--root", self.root, "--out", self.ids])
        with mock.patch.object(rp, "crawler_running", return_value=False):
            run(["enqueue", "--root", self.root, "--ids-file", self.ids, "--apply"])
        st = json.load(open(self.stp))
        self.assertEqual(sorted(ff.due_gallery_retries(st, limit=10)), ["11", "19"])


# Process lists as seen on Admin (Windows) and the Mac during the 2026-10-25 rollout (an_uong_quanh_day#1106).
VENV = r'"C:\script_foody\.venv\Scripts\python.exe"  -u "C:\script_foody\common\foody_fetch.py" --limit 0 --parity even'
CHILD = (r'"C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe" -u '
         r'"C:\script_foody\common\foody_fetch.py" --limit 0 --parity even')
WRAPPER = (r'cmd /c ""C:\script_foody\.venv\Scripts\python.exe" -u "C:\script_foody\common\foody_fetch.py" '
           r'--limit 0 --parity even >> "C:\script_foody\foody_data\fetch.log" 2>&1"')
OLD_HELPER = ("powershell -NoProfile -Command Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
              "'foody_fetch' } | Measure-Object | Select-Object -ExpandProperty Count")
SELF = r'"C:\script_foody\.venv\Scripts\python.exe" scripts\replay_missing_photos.py enqueue --ids-file x.tsv --apply'
MAC = ("/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/"
       "Contents/MacOS/Python -u /Users/x/script_foody/common/foody_fetch.py --limit 0 --parity odd")


class CrawlerDetection(unittest.TestCase):
    def test_old_windows_helper_is_not_a_crawler(self):
        # the bug: the PowerShell helper's own command line contains 'foody_fetch' -> always "running"
        self.assertEqual(rp.crawler_pids([(500, 400, OLD_HELPER), (400, 300, SELF)], exclude=(400, 300)), [])

    def test_real_windows_crawler_found(self):
        procs = [(11064, 1, WRAPPER), (9184, 11064, VENV), (11932, 9184, CHILD), (500, 400, OLD_HELPER),
                 (400, 300, SELF)]
        self.assertEqual(rp.crawler_pids(procs, exclude=(400, 300)), [9184, 11932])

    def test_own_pid_and_parent_excluded_even_if_they_match(self):
        self.assertEqual(rp.crawler_pids([(7, 6, VENV), (6, 5, CHILD), (8, 1, MAC)], exclude=(7, 6)), [8])

    def test_mac_framework_python_found(self):
        self.assertEqual(rp.crawler_pids([(8940, 1, MAC)]), [8940])

    def test_mentions_without_running_are_ignored(self):
        procs = [(1, 0, "vim common/foody_fetch.py"), (2, 0, "grep foody_fetch fetch.log"),
                 (3, 0, "tail -f /x/foody_data/fetch.log"), (4, 0, "python3 tests/test_foody_fetch.py"),
                 (5, 0, "python3 /x/common/foody_fetch.pyc"), (6, 0, "python3 scripts/replay_missing_photos.py enqueue"),
                 (7, 0, ""), (None, 0, MAC)]
        self.assertEqual(rp.crawler_pids(procs), [])

    def test_windows_listing_parsed_and_self_excluded(self):
        rows = [{"ProcessId": 11932, "ParentProcessId": 9184, "CommandLine": CHILD},
                {"ProcessId": os.getpid(), "ParentProcessId": os.getppid(), "CommandLine": VENV},
                {"ProcessId": 4, "ParentProcessId": 0, "CommandLine": None}]
        done = mock.Mock(stdout=json.dumps(rows))
        with mock.patch.object(rp.subprocess, "run", return_value=done) as run_:
            procs = rp._list_processes(nt=True)
        self.assertNotIn("foody_fetch", " ".join(run_.call_args[0][0]))   # the helper never names the crawler
        self.assertEqual(procs[0], (11932, 9184, CHILD))
        with mock.patch.object(rp, "_list_processes", return_value=procs):
            self.assertTrue(rp.crawler_running())
        with mock.patch.object(rp, "_list_processes", return_value=procs[1:]):
            self.assertFalse(rp.crawler_running())                    # only ourselves left -> not running

    def test_windows_single_process_json_object(self):
        done = mock.Mock(stdout=json.dumps({"ProcessId": 3, "ParentProcessId": 1, "CommandLine": CHILD}))
        with mock.patch.object(rp.subprocess, "run", return_value=done):
            self.assertEqual(rp._list_processes(nt=True), [(3, 1, CHILD)])

    def test_posix_listing_parsed(self):
        done = mock.Mock(stdout="  8940     1 " + MAC + "\n   12     1 /sbin/launchd\n  x y z\n")
        with mock.patch.object(rp.subprocess, "run", return_value=done):
            procs = rp._list_processes(nt=False)
        self.assertEqual(procs, [(8940, 1, MAC), (12, 1, "/sbin/launchd")])

    def test_listing_failure_is_unknown(self):
        with mock.patch.object(rp, "_list_processes", side_effect=OSError("no ps")):
            self.assertIsNone(rp.crawler_running())


if __name__ == "__main__":
    unittest.main()
