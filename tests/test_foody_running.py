"""Startup\\aqd_foody.cmd 'is foody already running?' check (after the 12/11 reboot the old check matched its own
command line, always said 'foody already running' and never started foody).

The real check is windows/foody_running.ps1 (CIM query); its match rule is mirrored in common/foody_running.py and
unit-tested here, plus static checks on the .ps1 and on the .cmd that windows/install.ps1 writes.
Run: python3 tests/test_foody_running.py
"""
import os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "common"))
import foody_running as fr  # noqa: E402

ROOT = "C:\\script_foody"
V = "C:\\script_foody\\.venv\\Scripts\\python.exe"
FETCH = f'"{V}" -u "C:\\script_foody\\common\\foody_fetch.py" --limit 0 --parity even'
CHECKER = (900, "powershell.exe", 'powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\script_foody\\windows\\foody_running.ps1" '
                                  '-Root "C:\\script_foody"')
PARENT = (800, "cmd.exe", 'C:\\WINDOWS\\system32\\cmd.exe /c ""C:\\Users\\Admin\\...\\Startup\\aqd_foody.cmd""')
WRAPPER = (810, "cmd.exe", f'cmd /c ""{V}" -u "C:\\script_foody\\common\\foody_fetch.py" --limit 0 --parity even '
                           '>> "C:\\script_foody\\foody_data\\fetch.log" 2>&1"')
# a checker that greps for the script name in its own arguments (what the old check effectively did)
SELF_GREP = (901, "python.exe", 'python -c "import sys; \'foody_fetch.py\' in sys.argv" foody_fetch.py')


class MatchTest(unittest.TestCase):
    def test_nothing_running_after_reboot(self):
        procs = [CHECKER, PARENT, WRAPPER, (1, "System", ""), (2, "explorer.exe", "C:\\WINDOWS\\explorer.exe")]
        self.assertEqual(fr.running_pids(procs, ROOT, exclude=(900, 800)), [])

    def test_checker_and_parent_never_match_even_if_python(self):
        self.assertFalse(fr.is_foody_process("python.exe", SELF_GREP[2], 901, ROOT, exclude=(901, 800)))
        self.assertFalse(fr.is_foody_process("powershell.exe", CHECKER[2] + " foody_fetch.py", 900, ROOT))
        self.assertFalse(fr.is_foody_process("cmd.exe", WRAPPER[2], 810, ROOT))

    def test_real_foody_matches(self):
        procs = [CHECKER, PARENT, WRAPPER, (4242, "python.exe", FETCH),
                 # the venv launcher's child: base interpreter, same script path
                 (4243, "python.exe", '"C:\\Users\\Admin\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" -u '
                                      '"C:\\script_foody\\common\\foody_fetch.py" --limit 0 --parity even')]
        self.assertEqual(fr.running_pids(procs, ROOT, exclude=(900, 800)), [4242, 4243])

    def test_started_by_hand_relative_path_matches(self):
        self.assertTrue(fr.is_foody_process("python.exe", "python common\\foody_fetch.py --limit 0", 7, ROOT))
        self.assertTrue(fr.is_foody_process("pythonw.exe", ".venv\\Scripts\\pythonw.exe -u common/foody_fetch.py", 7, ROOT))

    def test_other_root_and_other_scripts_do_not_match(self):
        self.assertFalse(fr.is_foody_process("python.exe", '"C:\\foody_test\\.venv\\Scripts\\python.exe" "C:\\foody_test\\common\\foody_fetch.py"', 7, ROOT))
        self.assertFalse(fr.is_foody_process("python.exe", '"C:\\script_foody\\common\\foody_push.py"', 7, ROOT))
        self.assertFalse(fr.is_foody_process("python.exe", 'C:\\aqd\\alt.py', 7, ROOT))
        self.assertTrue(fr.is_foody_process("python.exe", 'C:/SCRIPT_FOODY/common/foody_fetch.py', 7, "c:\\script_foody\\"))


def _read(rel):
    return open(os.path.join(REPO, rel), "rb").read()


class StaticTest(unittest.TestCase):
    def test_ps1_queries_only_real_python_and_excludes_itself(self):
        b = _read("windows/foody_running.ps1")
        self.assertTrue(all(c < 128 for c in b), "ASCII only (PowerShell 5.1 reads BOM-less .ps1 as ANSI)")
        self.assertIn(b"\r\n", b); self.assertNotIn(b"\n", b.replace(b"\r\n", b""))     # CRLF everywhere
        s = b.decode("ascii")
        self.assertIn("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\"", s)
        self.assertIn("$skip = @([int]$PID)", s)
        self.assertIn("$skip += [int]$me.ParentProcessId", s)
        self.assertIn("($skip -notcontains [int]$_.ProcessId)", s)
        self.assertIn("foody_fetch\\.py", s)
        self.assertNotRegex(s.lower(), r"wmic|tasklist|find(str)? ")
        # same rule as the Python mirror: absolute paths must be under the root, relative counts
        self.assertIn("[a-z]:/[^ ]*foody_fetch\\.py", s); self.assertIn(fr._ABS.pattern, s)
        exits = re.findall(r"exit (\d)", s)
        self.assertEqual(sorted(set(exits)), ["0", "1"])

    def test_generated_cmd_no_longer_self_matches(self):
        s = _read("windows/install.ps1").decode("utf-8")
        cmd = s[s.index("@\"\r\n@echo off"):s.index("\"@ | Set-Content -Path $Cmd")]
        low = cmd.lower()
        self.assertIn('foody_running.ps1" -root "$root"', low)
        self.assertIn("if %errorlevel% equ 0 exit /b 0", low)
        self.assertLess(low.index("foody_running.ps1"), low.index("start \"\" /min"))
        # no self-matching process search inside the .cmd itself
        self.assertNotRegex(low, r"wmic|tasklist|findstr|find \"|commandline like")
        self.assertTrue(all(ord(c) < 128 for c in cmd), "the .cmd is written with -Encoding ASCII")


if __name__ == "__main__":
    unittest.main(verbosity=1)
