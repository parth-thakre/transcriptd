"""Authentication for the HTTP routes and WebSockets.

A request gets in one of three ways:
- the bearer token (`Authorization: Bearer ...`, or `?token=` for older clients);
- a WebSocket ticket (`?ticket=`): single use, 60 s, minted by POST /v1/ws-ticket with the token,
  so the token itself never sits in a URL (and an access log);
- with `tailnet_auth`, the caller's Tailscale identity (tailnet.py). That one is ambient: a web
  page open in a browser on your phone carries it too. So a request let in by identity alone
  must not come from another site: if it has an `Origin` header, the origin must be this server;
  and it must be addressed (`Host`) to an IP address, localhost, a *.ts.net name or one of
  `allowed_hosts`. A DNS-rebinding page has a name of its own there, and for a same-origin GET
  the browser sends no Origin at all.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import tailnet

_bearer = HTTPBearer(auto_error=False)

TICKET_TTL_S = 60.0
WS_POLICY_VIOLATION = 1008
WS_UNAUTHORIZED = 4401


class TicketStore:
    """Single-use WebSocket tickets, in memory (a restart drops them; they live a minute anyway)."""

    def __init__(self, ttl_s: float = TICKET_TTL_S, max_live: int = 1000):
        self.ttl_s = ttl_s
        self.max_live = max_live
        self._lock = threading.Lock()
        self._live: dict[str, float] = {}  # ticket -> expires (monotonic)

    def issue(self) -> str:
        now = time.monotonic()
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            self._live = {t: exp for t, exp in self._live.items() if exp > now}
            if len(self._live) >= self.max_live:  # keep the newest; nobody needs a thousand at once
                for t in sorted(self._live, key=self._live.get)[: len(self._live) - self.max_live + 1]:
                    del self._live[t]
            self._live[ticket] = now + self.ttl_s
        return ticket

    def redeem(self, ticket: str | None) -> bool:
        """True once per issued ticket, within its lifetime."""
        if not ticket:
            return False
        with self._lock:
            exp = self._live.pop(ticket, None)
        return exp is not None and exp > time.monotonic()


def tickets(app) -> TicketStore:
    store = getattr(app.state, "ws_tickets", None)
    if store is None:
        store = app.state.ws_tickets = TicketStore()
    return store


def _ok(request_or_ws, presented: str | None) -> bool:
    expected = request_or_ws.app.state.settings.token
    return bool(presented) and secrets.compare_digest(presented, expected)


async def _tailnet_ok(request_or_ws) -> bool:
    """`tailnet_auth`: your own Tailscale devices need no token (see tailnet.py)."""
    s = request_or_ws.app.state.settings
    if not s.tailnet_auth:
        return False
    ip = request_or_ws.client.host if request_or_ws.client else None
    return await asyncio.to_thread(tailnet.trusted, ip, s.tailnet_users)


_DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}


def _split_host(host: str | None, scheme: str) -> tuple[str, str] | None:
    """(hostname, port) of a `Host` header, the port defaulting to the request's own scheme's."""
    if not host:
        return None
    try:
        h = urlsplit("//" + host.strip().lower())
        port = str(h.port) if h.port else _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    if not h.hostname or not port:
        return None
    return h.hostname, port


_WEB_SCHEME = {"http": "http", "ws": "http", "https": "https", "wss": "https"}


def same_origin(origin: str | None, host: str | None, scheme: str = "http") -> bool:
    """No Origin header (not a browser, or a same-origin navigation) passes; otherwise the
    origin must be the request's own: the same scheme as the request's (`scheme`; a WebSocket's
    ws/wss counts as http/https), and the host and port of the `Host` it was sent to (a Host
    without a port means the default port of `scheme`). "null" never matches. Behind a TLS
    proxy the request's scheme is https only with `trust_proxy_headers`; without it an https
    page's request needs the token, not identity alone."""
    if origin is None:
        return True
    try:
        o = urlsplit(origin.strip().lower())
        o_port = str(o.port) if o.port else _DEFAULT_PORTS.get(o.scheme)
    except ValueError:
        return False
    if o.scheme not in _WEB_SCHEME or not o.hostname or not o_port:
        return False
    if _WEB_SCHEME[o.scheme] != _WEB_SCHEME.get(scheme.lower()):
        return False
    h = _split_host(host, scheme)
    return h is not None and h == (o.hostname, o_port)


def host_allowed(host: str | None, allowed_hosts: list[str]) -> bool:
    """Is `host` (a Host header) a name a DNS-rebinding page can't have: an IP literal,
    localhost, a Tailscale MagicDNS name (*.ts.net), or one of `allowed_hosts`."""
    h = _split_host(host, "http")
    if h is None:
        return False
    name = h[0].strip("[]").rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return (name == "localhost" or name.endswith(".ts.net")
            or name in {a.strip().lower().rstrip(".") for a in allowed_hosts})


def _identity_ok(request_or_ws, scheme: str) -> bool:
    """A request let in by tailnet identity alone: sent to one of our own names, from our own page."""
    headers = request_or_ws.headers
    return (host_allowed(headers.get("host"), request_or_ws.app.state.settings.allowed_hosts)
            and same_origin(headers.get("origin"), headers.get("host"), scheme))


async def require_token(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    presented = creds.credentials if creds else request.query_params.get("token")
    if _ok(request, presented):
        return
    if await _tailnet_ok(request):
        if not _identity_ok(request, request.url.scheme):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-origin request or unknown host")
        return
    raise HTTPException(status_code=401, detail="invalid or missing token")


async def ws_reject(ws: WebSocket) -> int | None:
    """None if the socket may be accepted, else the close code: 4401 (no valid token, ticket or
    tailnet identity) or 1008 (identity only, from another site's page or to an unknown host)."""
    auth = ws.headers.get("authorization", "")
    presented = auth[7:] if auth.lower().startswith("bearer ") else ws.query_params.get("token")
    if _ok(ws, presented) or tickets(ws.app).redeem(ws.query_params.get("ticket")):
        return None
    if await _tailnet_ok(ws):
        return None if _identity_ok(ws, ws.url.scheme) else WS_POLICY_VIOLATION
    return WS_UNAUTHORIZED


def state(request: Request):
    return request.app.state
