"""Match rule of windows/foody_running.ps1 (called by Startup\\aqd_foody.cmd before it starts foody), in Python so it
can be unit-tested on any OS (tests/test_foody_running.py). Keep both in sync.

A process is "foody running for <root>" when:
  - its image name starts with "python" (python.exe / pythonw.exe / python3.12.exe) - so the checking powershell,
    its cmd.exe parent and the `cmd /c "...python.exe" -u ...foody_fetch.py` wrapper never count;
  - it is not the checker itself or its parent (exclude);
  - its command line has foody_fetch.py, and every absolute path to foody_fetch.py in it is under <root> (a relative
    `python common\\foody_fetch.py` started by hand in the root counts; another root's foody does not).
"""
import re

_ABS = re.compile(r"[a-z]:/[^ ]*foody_fetch\.py")


def _n(p):
    return str(p or "").replace("\\", "/").replace('"', "").lower()


def is_foody_process(name, cmdline, pid, root, exclude=()):
    if pid in set(exclude):
        return False
    if not _n(name).startswith("python"):
        return False
    c = _n(cmdline)
    if "foody_fetch.py" not in c:
        return False
    abs_paths = _ABS.findall(c)
    if not abs_paths:
        return True
    root_n = _n(root).rstrip("/") + "/"
    return any(p.startswith(root_n) for p in abs_paths)


def running_pids(procs, root, exclude=()):
    """procs: [(pid, name, cmdline)] -> PIDs of foody for this root."""
    return [pid for pid, name, cmd in procs if is_foody_process(name, cmd, pid, root, exclude)]
