"""scripts/repair_coordinates.py (script_foody#18): patch only when Foody now gives a valid point; never overwrite a valid one.
No network. Run: python3 tests/test_repair_coordinates.py"""
import os, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common")); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
os.environ.setdefault("AQD_FOODY_DATA", tempfile.mkdtemp(prefix="foody18r_"))
import repair_coordinates as rc  # noqa: E402


def row(rid=129925):
    return {"restaurant_id": rid, "source_id": "101647", "url": "https://www.foody.vn/ho-chi-minh/com-tam-chu-thai"}


class PlanTest(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(rc.plan(10.755671, 106.722071, 0.755671, 106.722071), "patch")
        self.assertEqual(rc.plan(0.755671, 106.722071, 0.755671, 106.722071), "manual")  # Foody still wrong
        self.assertEqual(rc.plan(10.7433766, 10.7433766, 10.7433766, 10.7433766), "manual")
        self.assertEqual(rc.plan(10.755671, 106.722071, 10.7, 106.7), "ok")  # valid server point is never touched

    def test_run_dry_run_then_apply(self):
        patch, lines = mock.Mock(), []
        det = lambda r: {"lat": 10.755671, "lng": 106.722071, "address": "36 Huỳnh Tấn Phát"}
        st = rc.run([row()], det, lambda rid: (0.755671, 106.722071), patch, apply=False, out=lines.append)
        self.assertEqual((st["patch"], st["patched"]), (1, 0)); patch.assert_not_called()
        st = rc.run([row()], det, lambda rid: (0.755671, 106.722071), patch, apply=True, out=lines.append)
        patch.assert_called_once_with(129925, 10.755671, 106.722071); self.assertEqual(st["patched"], 1)

    def test_manual_rows_are_listed_and_not_patched(self):
        patch, lines = mock.Mock(), []
        det = lambda r: {"lat": 0.0, "lng": 0.0, "address": "A"}
        st = rc.run([row()], det, lambda rid: (0.0, 0.0), patch, apply=True, out=lines.append)
        self.assertEqual(st["manual"], 1); patch.assert_not_called()
        self.assertIn("com-tam-chu-thai", lines[0])

    def test_one_failure_does_not_stop_the_rest(self):
        def det(r):
            if r["restaurant_id"] == 1: raise RuntimeError("boom")
            return {"lat": 10.7, "lng": 106.7}
        st = rc.run([row(1), row(2)], det, lambda rid: (0, 0), mock.Mock(), apply=False, out=lambda m: None)
        self.assertEqual((st["fail"], st["patch"]), (1, 1))

    def test_ids_file_parses_all_eleven(self):
        rows = rc.read_rows(os.path.join(os.path.dirname(HERE), "scripts", "replay_ids", "foody_bad_coords_1452.tsv"))
        self.assertEqual(len(rows), 11)
        self.assertEqual({r["restaurant_id"] for r in rows}, {103921, 108846, 118971, 129925, 131548, 131978, 138077, 144747, 151971, 155268, 158982})


if __name__ == "__main__":
    unittest.main()
