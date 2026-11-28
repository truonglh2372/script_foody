#!/usr/bin/env python3
"""Send the crawler's server calls straight to the origin IP, bypassing Cloudflare (AQD_ORIGIN_IP).

Why: the Viettel -> Cloudflare SG route drops packets, so API calls through Cloudflare are slow; the origin answers
fast. See truonglh2372/an_uong_quanh_day#1058.

How it works, like `curl --resolve anuongquanhday.com:443:<ip>`: only the DNS lookup of the AQD_BASE host is replaced
by the IP. The URL, the TLS SNI, the Host header and the certificate check all keep the host name, so the origin's
normal certificate must match it (it does: kamal-proxy serves a Let's Encrypt cert for anuongquanhday.com). TLS
verification is never turned off, and the Authorization header/token is unchanged.

- AQD_ORIGIN_IP empty or unset -> nothing changes (normal DNS through Cloudflare). This is the default.
- AQD_ORIGIN_IP is one IPv4/IPv6 literal, or a host name such as origin.anuongquanhday.com (a DNS-only record, so the
  server can move without editing every machine). A host name is resolved with the REAL resolver (never through this
  pin), cached, and re-resolved every RESOLVE_EVERY_S (5 min); when a lookup fails the last good IP stays in use and a
  warning is logged (first lookup failing = normal DNS for AQD_BASE). AQD_BASE must be https, otherwise OriginPinError
  (a ValueError).
- Only that one host name is pinned; Google, Foody, photo CDNs etc. resolve normally.
- Rollback: delete the AQD_ORIGIN_IP line from creds.env (or the environment) and restart the crawler.
"""
import ipaddress, logging, os, re, socket, threading, time
from urllib.parse import urlsplit

ENV = "AQD_ORIGIN_IP"
RESOLVE_EVERY_S = 300
_REAL_GETADDRINFO = socket.getaddrinfo
_PINS = {}  # lower-case host name -> {"target": IP literal or host name, "ip": current IP or None, "at": last lookup}
_LOCK = threading.Lock()
_now = time.monotonic
_log = logging.getLogger("origin_pin")
_NAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class OriginPinError(ValueError):
    pass


def parse(base, ip):
    """(host, target) to pin, or None when ip is empty; target is an IP literal or a host name. Raises OriginPinError
    on anything else or a non-https base."""
    ip = (ip or "").strip()
    if not ip:
        return None
    try:
        if "%" in ip:  # an IPv6 zone id (fe80::1%en0) is a local-link address, never the origin
            raise ValueError(ip)
        address = str(ipaddress.ip_address(ip))
    except ValueError:
        # not an IP: accept a plain host name, but not something that merely looks like a (broken) IPv4 address
        if not _NAME.match(ip) or re.fullmatch(r"[0-9.]+", ip):
            raise OriginPinError(f"{ENV} must be a single IP address (for example 203.0.113.10), a host name, or empty") from None
        address = ip.lower().rstrip(".")
    parts = urlsplit((base or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise OriginPinError(f"{ENV} only works with an https AQD_BASE, so the certificate is still checked")
    return parts.hostname.lower().rstrip("."), address


def _resolve(entry, host):
    """Look the origin host name up with the real resolver (never the pin). Keeps the last good IP on failure."""
    try:
        infos = _REAL_GETADDRINFO(entry["target"], None, 0, socket.SOCK_STREAM)
        entry["ip"] = infos[0][4][0]
    except (OSError, IndexError) as e:
        _log.warning("cannot resolve %s=%s (%s); %s", ENV, entry["target"], type(e).__name__,
                     f"keeping {entry['ip']}" if entry["ip"] else f"using normal DNS for {host}")
    entry["at"] = _now()


def pinned(host):
    """The IP a host name is pinned to (re-resolving a host-name origin when its cache is 5 minutes old), or None."""
    key = (host or "").lower().rstrip(".")
    entry = _PINS.get(key)
    if entry is None:
        return None
    if entry["at"] is not None and entry["ip"] is not None and not entry["name"]:
        return entry["ip"]
    if entry["name"]:
        with _LOCK:
            if entry["at"] is None or _now() - entry["at"] >= RESOLVE_EVERY_S:
                _resolve(entry, key)
    return entry["ip"]


def _getaddrinfo(host, *args, **kwargs):
    if isinstance(host, (bytes, bytearray)):
        key = bytes(host).decode("ascii", "ignore")
    else:
        key = host if isinstance(host, str) else ""
    ip = pinned(key)
    return _REAL_GETADDRINFO(ip if ip else host, *args, **kwargs)


def install(base, ip=None):
    """Pin the host of `base` to `ip` (default: $AQD_ORIGIN_IP) for this process. Returns (host, target) or None."""
    pin = parse(base, os.environ.get(ENV) if ip is None else ip)
    if pin is None:
        return None
    host, target = pin
    try:
        ipaddress.ip_address(target)
        entry = {"target": target, "ip": target, "at": _now(), "name": False}
    except ValueError:
        entry = {"target": target, "ip": None, "at": None, "name": True}
    with _LOCK:
        _PINS[host] = entry
        if entry["name"]:
            _resolve(entry, host)
        if socket.getaddrinfo is not _getaddrinfo:
            socket.getaddrinfo = _getaddrinfo
    return pin


def reset():
    """Drop every pin and restore the real resolver (tests)."""
    with _LOCK:
        _PINS.clear()
        socket.getaddrinfo = _REAL_GETADDRINFO
