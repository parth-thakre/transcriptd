"""Tailnet identity: let your own Tailscale devices in without the token.

With `tailnet_auth = true`, a request whose source address is a Tailscale peer is allowed when
`tailscale whois` says the peer is logged in as the same user as this machine (or as one of
`tailnet_users`). Other people's devices on the tailnet, shared nodes and tagged devices
still need the token. Connections from this machine itself are never trusted this way: that
is also where `tailscale serve` proxies from, and it would vouch for anyone it fronts.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
import threading
import time

log = logging.getLogger(__name__)

_TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_TTL_S = 300.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, str | None]] = {}  # ip -> (expires, login)
_self: tuple[float, str | None, frozenset[str]] = (0.0, None, frozenset())  # (expires, login, own ips)


def _run(*args: str) -> dict | None:
    try:
        out = subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=3, check=True).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("tailscale %s failed: %s", " ".join(args), exc)
        return None


def _me() -> tuple[str | None, frozenset[str]]:
    global _self
    with _lock:
        if _self[0] > time.monotonic():
            return _self[1], _self[2]
    s = _run("status", "--json", "--peers=false")
    login, ips = None, frozenset()
    if s and s.get("Self"):
        login = (s.get("User") or {}).get(str(s["Self"].get("UserID")), {}).get("LoginName")
        ips = frozenset(s["Self"].get("TailscaleIPs") or [])
    with _lock:
        _self = (time.monotonic() + (_TTL_S if login else 30.0), login, ips)
    return login, ips


def login_of(ip: str) -> str | None:
    """The Tailscale login of the device at `ip`, or None (not a peer, tagged, or unknown)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(ip)
        if hit and hit[0] > now:
            return hit[1]
    w = _run("whois", "--json", ip)
    login = None
    if w and not (w.get("Node") or {}).get("Tags"):
        login = (w.get("UserProfile") or {}).get("LoginName")
    with _lock:
        _cache[ip] = (now + (_TTL_S if login else 30.0), login)
    return login


def trusted(ip: str | None, allowed_users: list[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not any(addr in net for net in _TAILNET):
        return False
    me, own_ips = _me()
    if ip in own_ips:
        return False
    allowed = set(allowed_users) or ({me} if me else set())
    login = login_of(ip)
    return login is not None and login in allowed
