"""AQD_ORIGIN_IP may be a host name (script_foody#32): resolved with the REAL resolver, cached, re-resolved every
5 minutes, last good IP kept when a lookup fails. No network: the real resolver and the clock are mocked.

Run: python3 tests/test_origin_host.py
"""
import os, socket, sys, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import origin_pin  # noqa: E402

BASE = "https://anuongquanhday.com"
ORIGIN = "origin.anuongquanhday.com"


def answer(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class OriginHostTest(unittest.TestCase):
    def setUp(self):
        origin_pin.reset()
        self.addCleanup(origin_pin.reset)
        self.clock = Clock()
        for target, value in (("_now", self.clock),):
            p = mock.patch.object(origin_pin, target, value); p.start(); self.addCleanup(p.stop)
        self.real = mock.Mock(side_effect=lambda host, *a, **k: answer("203.0.113.7") if host == ORIGIN else answer("198.51.100.1"))
        p = mock.patch.object(origin_pin, "_REAL_GETADDRINFO", self.real); p.start(); self.addCleanup(p.stop)

    def lookups(self, name=ORIGIN):
        return [c for c in self.real.call_args_list if c.args[0] == name]

    def test_host_name_is_accepted_and_ip_literal_unchanged(self):
        self.assertEqual(("anuongquanhday.com", ORIGIN), origin_pin.parse(BASE, ORIGIN))
        self.assertEqual(("anuongquanhday.com", "203.0.113.10"), origin_pin.parse(BASE, "203.0.113.10"))
        self.assertIsNone(origin_pin.parse(BASE, ""))

    def test_garbage_is_still_refused(self):
        for value in ("10.0.0.0/8", "1.2.3", "fe80::1%en0", "1.2.3.4 5.6.7.8", "-bad.example", "a_b.example", "http://x"):
            with self.assertRaises(origin_pin.OriginPinError, msg=value):
                origin_pin.parse(BASE, value)

    def test_host_name_resolves_with_the_real_resolver_and_pins(self):
        origin_pin.install(BASE, ORIGIN)
        self.real.reset_mock()
        socket.getaddrinfo("anuongquanhday.com", 443)
        self.assertEqual("203.0.113.7", origin_pin.pinned("anuongquanhday.com"))
        # the final connect lookup goes to the resolved IP, the origin name was looked up un-pinned
        self.assertIn("203.0.113.7", [c.args[0] for c in self.real.call_args_list])
        self.assertNotIn("anuongquanhday.com", [c.args[0] for c in self.real.call_args_list])

    def test_result_is_cached_for_five_minutes_then_re_resolved(self):
        origin_pin.install(BASE, ORIGIN)
        for _ in range(5):
            origin_pin.pinned("anuongquanhday.com")
        self.assertEqual(1, len(self.lookups()))
        self.clock.t += 299
        origin_pin.pinned("anuongquanhday.com")
        self.assertEqual(1, len(self.lookups()))
        self.clock.t += 2  # 301 s
        self.real.side_effect = lambda host, *a, **k: answer("203.0.113.99") if host == ORIGIN else answer("198.51.100.1")
        self.assertEqual("203.0.113.99", origin_pin.pinned("anuongquanhday.com"))
        self.assertEqual(2, len(self.lookups()))

    def test_failed_lookup_keeps_last_good_ip_and_warns(self):
        origin_pin.install(BASE, ORIGIN)
        self.clock.t += 301
        self.real.side_effect = socket.gaierror("no dns")
        with self.assertLogs("origin_pin", "WARNING"):
            self.assertEqual("203.0.113.7", origin_pin.pinned("anuongquanhday.com"))
        # the failure is retried later, not every call
        n = len(self.lookups())
        origin_pin.pinned("anuongquanhday.com")
        self.assertEqual(n, len(self.lookups()))

    def test_first_lookup_failing_falls_back_to_normal_dns(self):
        self.real.side_effect = socket.gaierror("no dns")
        with self.assertLogs("origin_pin", "WARNING"):
            origin_pin.install(BASE, ORIGIN)
        self.assertIsNone(origin_pin.pinned("anuongquanhday.com"))
        self.real.side_effect = lambda host, *a, **k: answer("198.51.100.1")
        socket.getaddrinfo("anuongquanhday.com", 443)  # unpinned: the normal name goes to the real resolver
        self.assertEqual("anuongquanhday.com", self.real.call_args.args[0])

    def test_ip_literal_never_calls_the_resolver_for_the_origin(self):
        origin_pin.install(BASE, "203.0.113.10")
        self.assertEqual("203.0.113.10", origin_pin.pinned("anuongquanhday.com"))
        self.assertEqual([], self.lookups("203.0.113.10"))


if __name__ == "__main__":
    unittest.main()
