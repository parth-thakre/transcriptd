"""Auth, WebSocket tickets, cross-origin checks for tailnet identity, request limits, log
redaction and the token file (api/deps.py, app.py, cli.py, config.py)."""

import asyncio
import io
import json
import logging
import os
import stat
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from starlette.websockets import WebSocketDisconnect

from tests.test_api import _wav_bytes, client  # noqa: F401  (fixture)
from transcriptd import cli
from transcriptd.api import deps, dictate, routes
from transcriptd.api.dictate import _close_partials, _partial
from transcriptd.config import Settings, _ensure_token


def _ws_close_code(client, url, **kw) -> int:  # noqa: F811
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(url, **kw) as ws:
        ws.receive_json()
    return exc.value.code


def _dictate_once(ws):
    ws.send_text(json.dumps({"type": "start", "profile": "english"}))
    assert ws.receive_json()["type"] == "ready"
    ws.send_bytes(np.zeros(16000, dtype="<i2").tobytes())
    ws.send_text(json.dumps({"type": "end"}))
    return ws.receive_json()


# -- WebSocket tickets --------------------------------------------------------------------


def test_a_ws_ticket_opens_one_socket_once(client):  # noqa: F811
    j = client.post("/v1/ws-ticket").json()
    assert j["expires_in"] == 60 and len(j["ticket"]) >= 32
    client.headers.pop("Authorization")
    with client.websocket_connect(f"/v1/dictate?ticket={j['ticket']}") as ws:
        assert _dictate_once(ws)["type"] == "final"
    assert _ws_close_code(client, f"/v1/dictate?ticket={j['ticket']}") == 4401  # single use
    assert _ws_close_code(client, "/v1/dictate?ticket=made-up") == 4401
    assert _ws_close_code(client, "/v1/dictate") == 4401
    assert client.post("/v1/ws-ticket").status_code == 401  # minting one needs auth


def test_ws_tickets_expire(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(deps.time, "monotonic", lambda: now[0])
    store = deps.TicketStore(ttl_s=60)
    old, fresh = store.issue(), store.issue()
    now[0] += 61
    assert not store.redeem(old)
    assert not store.redeem(None) and not store.redeem("")
    t = store.issue()
    now[0] += 59
    assert store.redeem(t) and not store.redeem(t)
    assert not store.redeem(fresh)


def test_ws_ticket_store_stays_bounded():
    store = deps.TicketStore(max_live=3)
    issued = [store.issue() for _ in range(5)]
    assert len(store._live) == 3
    assert not store.redeem(issued[0]) and store.redeem(issued[-1])


def test_the_token_in_the_ws_url_still_works_for_older_clients(client):  # noqa: F811
    token = client.headers.pop("Authorization")[7:]
    with client.websocket_connect(f"/v1/dictate?token={token}") as ws:
        assert _dictate_once(ws)["type"] == "final"


# -- tailnet identity must not be usable cross-site --------------------------------------


def test_same_origin():
    assert deps.same_origin(None, "testserver")
    assert deps.same_origin("http://testserver", "testserver")
    assert deps.same_origin("https://box.example.ts.net", "box.example.ts.net:443", "https")
    assert deps.same_origin("https://box.example.ts.net", "box.example.ts.net", "https")
    assert deps.same_origin("https://box.example.ts.net", "box.example.ts.net", "wss")
    # A Host without a port means the port of the scheme the request came in on, not the Origin's.
    assert not deps.same_origin("https://box.example.ts.net", "box.example.ts.net", "http")
    assert not deps.same_origin("http://box.example.ts.net", "box.example.ts.net", "https")
    assert deps.same_origin("https://box.example.ts.net:7443", "BOX.example.ts.net:7443", "https")
    assert deps.same_origin("https://box.example.ts.net:7443", "box.example.ts.net:7443", "wss")
    assert deps.same_origin("http://100.64.0.1:7411", "100.64.0.1:7411", "ws")
    # Same host and port but another scheme is another origin, explicit port or not.
    assert not deps.same_origin("http://box.example.ts.net:7443", "box.example.ts.net:7443", "https")
    assert not deps.same_origin("http://box.example.ts.net:7443", "box.example.ts.net:7443", "wss")
    assert not deps.same_origin("https://100.64.0.1:7411", "100.64.0.1:7411", "http")
    assert not deps.same_origin("https://100.64.0.1:7411", "100.64.0.1:7411", "ws")
    assert not deps.same_origin("https://box.example.ts.net", "box.example.ts.net:443")
    assert deps.same_origin("http://100.64.0.1:7411", "100.64.0.1:7411")
    assert not deps.same_origin("http://100.64.0.1:7412", "100.64.0.1:7411")
    assert not deps.same_origin("https://evil.example", "box.example.ts.net")
    assert not deps.same_origin("http://box.example.ts.net", "box.example.ts.net:443")
    assert not deps.same_origin("null", "testserver")
    assert not deps.same_origin("file://", "testserver")
    assert not deps.same_origin("http://testserver", None)
    assert not deps.same_origin("http://testserver:99999", "testserver")


def test_host_allowed():
    for host in ("100.64.0.1:7411", "100.64.0.1", "[fd7a:115c:a1e0::1]:7411", "127.0.0.1:7411", "localhost:7411",
                 "LOCALHOST", "box.tail1234.ts.net", "box.tail1234.ts.net.:7443"):
        assert deps.host_allowed(host, []), host
    for host in ("evil.example:7411", "evil.example", "ts.net.evil.example", "evil-ts.net", "box", "", None,
                 "100.64.0.1:99999"):
        assert not deps.host_allowed(host, []), host
    assert deps.host_allowed("box:7411", ["box"]) and deps.host_allowed("Box.LAN", ["box.lan"])


@pytest.fixture
def tailnet_client(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(deps.tailnet, "trusted", lambda ip, users: True)
    client.app.state.settings.tailnet_auth = True
    client.app.state.settings.allowed_hosts = ["testserver"]  # the TestClient's Host
    token = client.headers.pop("Authorization")
    return client, token


def test_tailnet_identity_refuses_dns_rebinding(tailnet_client):
    """A page on evil.example whose name now resolves to the server: same "origin" as far as
    Origin vs Host go, but the Host is a name that isn't ours."""
    client, token = tailnet_client  # noqa: F811
    client.app.state.settings.allowed_hosts = []
    rebound = {"Host": "evil.example:17499", "Origin": "http://evil.example:17499"}
    assert client.post("/v1/ws-ticket", headers=rebound).status_code == 403
    # A same-origin GET carries no Origin at all.
    assert client.get("/v1/dictations", headers={"Host": "evil.example:17499"}).status_code == 403
    assert _ws_close_code(client, "/v1/dictate", headers=rebound) == 1008
    assert client.get("/v1/health").status_code == 403  # "testserver" isn't listed any more
    # Our own names: an IP literal, a MagicDNS name, localhost, or one in allowed_hosts.
    for host in ("100.64.0.1:17499", "box.tail1234.ts.net:17499", "localhost:17499"):
        assert client.post("/v1/ws-ticket", headers={"Host": host, "Origin": f"http://{host}"}).status_code == 200, host
        assert client.get("/v1/dictations", headers={"Host": host}).status_code == 200, host
    with client.websocket_connect("/v1/dictate", headers={"Host": "100.64.0.1:17499"}) as ws:
        assert _dictate_once(ws)["type"] == "final"
    client.app.state.settings.allowed_hosts = ["evil.example"]
    assert client.post("/v1/ws-ticket", headers=rebound).status_code == 200
    # The token is unaffected: whatever the Host, it's a deliberate credential.
    client.app.state.settings.allowed_hosts = []
    assert client.post("/v1/ws-ticket", headers={**rebound, "Authorization": token}).status_code == 200


def test_tailnet_identity_refuses_other_sites(tailnet_client):
    client, token = tailnet_client  # noqa: F811
    body = {"term": "Hyprland"}
    assert client.post("/v1/vocab", json=body).status_code == 201  # no Origin: an app, not a page
    assert client.post("/v1/vocab", json=body, headers={"Origin": "http://testserver"}).status_code == 201
    r = client.post("/v1/vocab", json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.delete("/v1/vocab/1", headers={"Origin": "null"}).status_code == 403
    assert client.post("/v1/meetings", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/v1/ws-ticket", headers={"Origin": "https://evil.example"}).status_code == 403
    # Our host and port, another scheme: a different origin (the request came in over http).
    assert client.post("/v1/vocab", json=body, headers={"Origin": "https://testserver:80"}).status_code == 403
    # The token is a deliberate credential, not ambient: other origins are its client's business.
    r = client.post("/v1/vocab", json=body, headers={"Origin": "https://evil.example", "Authorization": token})
    assert r.status_code == 201


def test_tailnet_identity_refuses_other_sites_websockets(tailnet_client):
    client, token = tailnet_client  # noqa: F811
    evil = {"Origin": "https://evil.example"}
    assert _ws_close_code(client, "/v1/dictate", headers=evil) == 1008
    mid = client.post("/v1/meetings", json={}).json()["id"]
    assert _ws_close_code(client, f"/v1/meetings/{mid}/stream", headers=evil) == 1008
    assert _ws_close_code(client, "/v1/dictate", headers={"Origin": "https://testserver:80"}) == 1008
    with client.websocket_connect("/v1/dictate", headers={"Origin": "http://testserver"}) as ws:
        assert _dictate_once(ws)["type"] == "final"
    with client.websocket_connect("/v1/dictate", headers={**evil, "Authorization": token}) as ws:
        assert _dictate_once(ws)["type"] == "final"


# -- limits -------------------------------------------------------------------------------


def test_oversized_uploads_are_refused(client):  # noqa: F811
    client.app.state.settings.max_upload_mb = 16 / 1024  # 16 KB; a 1 s take is ~32 KB
    r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 413
    # Without a Content-Length (chunked): refused once the bytes received pass the limit.
    data = _wav_bytes()

    def chunks():
        for i in range(0, len(data), 4096):
            yield data[i:i + 4096]

    r = client.post("/v1/vocab/import", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(0.2), "audio/wav")}).status_code == 200


def test_a_take_longer_than_the_limit_is_refused(client):  # noqa: F811
    client.app.state.settings.max_dictation_seconds = 1
    with client.websocket_connect("/v1/dictate") as ws:
        ws.send_text(json.dumps({"type": "start", "profile": "english"}))
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(np.zeros(16000, dtype="<i2").tobytes())
        ws.send_bytes(np.zeros(1600, dtype="<i2").tobytes())
        assert ws.receive_json() == {"type": "error", "message": "utterance too long"}


def test_transcribe_uploads_longer_than_a_take_are_refused(client):  # noqa: F811
    import io

    import soundfile as sf

    client.app.state.settings.max_dictation_seconds = 2
    r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(3), "audio/wav")})
    assert r.status_code == 413
    # FLAC squeezes silence to almost nothing: the length, not the size, is what's bounded.
    buf = io.BytesIO()
    sf.write(buf, np.zeros((16000 * 60, 2), dtype=np.float32), 16000, format="FLAC")
    assert len(buf.getvalue()) < 100_000
    r = client.post("/v1/transcribe", files={"file": ("a.flac", buf.getvalue(), "audio/flac")})
    assert r.status_code == 413
    assert client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(1.5), "audio/wav")}).status_code == 200
    stereo = io.BytesIO()
    sf.write(stereo, np.full((44100, 2), 0.25, dtype=np.float32), 44100, format="WAV")
    pcm = routes.decode_upload(stereo.getvalue(), 2)  # mixed to mono, resampled to 16 kHz
    assert abs(pcm.size - 16000) < 10 and abs(float(pcm.mean()) - 0.25) < 0.01


class _BlockingStream:
    """A partials stream whose native feed blocks until `release` is set."""

    def __init__(self):
        self.release = threading.Event()
        self.feeding = threading.Event()
        self.closed = threading.Event()
        self.closed_while_feeding = False

    def feed(self, pcm):
        self.feeding.set()
        self.release.wait(10)
        self.feeding.clear()
        return "hello"

    def close(self):
        self.closed_while_feeding = self.feeding.is_set()
        self.closed.set()


async def _feed_started(stream):
    while not stream.feeding.is_set():
        await asyncio.sleep(0.005)


def test_a_partials_stream_is_never_closed_under_a_running_feed():
    async def run():
        sent = []

        async def send(obj):
            sent.append(obj)

        # Cancelling the partial task doesn't stop the thread; closing waits for the feed.
        s = _BlockingStream()
        task = asyncio.create_task(_partial(s, np.zeros(320, dtype=np.float32), send, [""]))
        await _feed_started(s)
        task.cancel()
        closer = asyncio.create_task(_close_partials(task, s, warn_after=0.01))
        await asyncio.sleep(0.1)
        assert not task.done() and not s.closed.is_set()  # the task outlives its cancel until the feed returns
        s.release.set()
        await closer
        assert task.cancelled() and s.closed.is_set() and not s.closed_while_feeding

        # The closer itself cancelled (the socket handler torn down): the close still comes, after the feed.
        s = _BlockingStream()
        task = asyncio.create_task(_partial(s, np.zeros(320, dtype=np.float32), send, [""]))
        await _feed_started(s)
        closer = asyncio.create_task(_close_partials(task, s))
        await asyncio.sleep(0.05)
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert not s.closed.is_set()
        s.release.set()
        for _ in range(200):
            if s.closed.is_set():
                break
            await asyncio.sleep(0.01)
        assert s.closed.is_set() and not s.closed_while_feeding
        assert sent == [{"type": "partial", "text": "hello"}]  # the uncancelled feed's text went out

        await _close_partials(None, None)

    asyncio.run(run())


def test_a_partials_close_queued_behind_busy_workers_still_runs_if_the_handler_is_cancelled():
    """The handler hands the stream to _close_partials, which hands the close to a worker thread.
    With every worker busy the close waits in the queue; cancelling the handler then must not
    drop it, or the engine's lock is never released."""
    from concurrent.futures import ThreadPoolExecutor

    async def run():
        pool = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(pool)
        busy = threading.Event()
        blocker = loop.run_in_executor(None, busy.wait, 10)  # the only worker, held
        closes = []
        stream = dictate._OnceClosed(SimpleNamespace(close=lambda: closes.append(1)))

        closer = asyncio.create_task(_close_partials(None, stream))
        await asyncio.sleep(0.05)
        assert closes == []  # queued behind the blocker
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        busy.set()
        await blocker
        for _ in range(200):
            if closes:
                break
            await asyncio.sleep(0.01)
        await loop.run_in_executor(None, lambda: None)  # drained: anything queued has run
        assert closes == [1]
        pool.shutdown(wait=True)

    asyncio.run(run())


def test_a_dropped_socket_waits_for_the_feed_before_closing_partials(client, monkeypatch):  # noqa: F811
    s = _BlockingStream()
    monkeypatch.setattr(dictate, "_open_partials", lambda st, name, language: s)
    with client.websocket_connect("/v1/dictate") as ws:
        ws.send_text(json.dumps({"type": "start", "profile": "english", "partials": True}))
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(np.zeros(320, dtype="<i2").tobytes())
        assert s.feeding.wait(5)
        threading.Timer(0.3, s.release.set).start()
        t0 = time.monotonic()
    # The handler has wound down: it closed the stream, and only once the feed had returned.
    assert s.closed.wait(5) and not s.closed_while_feeding
    assert time.monotonic() - t0 >= 0.25


class _CountingStream(_BlockingStream):
    def __init__(self):
        super().__init__()
        self.closes = 0

    def close(self):
        self.closes += 1
        super().close()


class _FakeSocket:
    """Just enough of a WebSocket to drive dictate() by hand, so it can be cancelled mid-command."""

    def __init__(self, state):
        self.app = SimpleNamespace(state=state)
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def accept(self):
        pass

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def text(self, obj):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(obj)})

    def audio(self):
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": np.zeros(320, dtype="<i2").tobytes()})


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def test_a_partials_stream_is_closed_exactly_once(client, monkeypatch):  # noqa: F811
    """Each close resets the native stream and releases its engine's lock: a second close would
    release the lock the next take holds. Normal end, a cancel while end waits for the feed, a
    disconnect mid-feed, and a cancel while the stream is opening each close it once."""
    streams: list[_CountingStream] = []

    def open_partials(st, name, language):
        streams.append(_CountingStream())
        return streams[-1]

    async def admit(ws):
        return None

    monkeypatch.setattr(dictate, "_open_partials", open_partials)
    monkeypatch.setattr(dictate, "ws_reject", admit)
    start = {"type": "start", "profile": "english", "partials": True}

    async def take(ws):
        ws.text(start)
        ws.audio()
        await _until(lambda: streams and streams[-1].feeding.is_set())
        return streams[-1]

    async def run():
        # Normal end, then the socket closes: the end closed it; the finally has nothing left.
        ws = _FakeSocket(client.app.state)
        handler = asyncio.create_task(dictate.dictate(ws))
        s = await take(ws)
        s.release.set()
        await _until(lambda: any(m["type"] == "partial" for m in ws.sent))
        ws.text({"type": "end"})
        await _until(lambda: any(m["type"] == "final" for m in ws.sent))
        ws.inbox.put_nowait({"type": "websocket.disconnect"})
        await handler
        assert s.closes == 1

        # Cancelled while end waits for the feed: the close deferred to the feed's end is the only one.
        ws = _FakeSocket(client.app.state)
        handler = asyncio.create_task(dictate.dictate(ws))
        s = await take(ws)
        ws.text({"type": "end"})
        await asyncio.sleep(0.05)
        handler.cancel()
        s.release.set()
        with pytest.raises(asyncio.CancelledError):
            await handler
        await _until(lambda: s.closes >= 1)
        await asyncio.sleep(0.1)
        assert s.closes == 1 and not s.closed_while_feeding

        # The socket drops mid-feed: the finally closes it once the feed returns.
        ws = _FakeSocket(client.app.state)
        handler = asyncio.create_task(dictate.dictate(ws))
        s = await take(ws)
        ws.inbox.put_nowait({"type": "websocket.disconnect"})
        await asyncio.sleep(0.05)
        s.release.set()
        await handler
        assert s.closes == 1 and not s.closed_while_feeding

        # Cancelled while the stream opens (it takes the engine's lock): closed once it has opened.
        opening, opened = threading.Event(), threading.Event()

        def slow_open(st, name, language):
            opening.set()
            opened.wait(5)
            return open_partials(st, name, language)

        monkeypatch.setattr(dictate, "_open_partials", slow_open)
        task = asyncio.create_task(dictate._open_stream(client.app.state, "english", None))
        await _until(opening.is_set)
        task.cancel()
        opened.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: streams[-1].closes >= 1)
        await asyncio.sleep(0.05)
        assert streams[-1].closes == 1

    asyncio.run(run())


def test_decoding_an_upload_holds_about_the_16k_result_in_memory():
    import io
    import tracemalloc

    import soundfile as sf
    import soxr

    rate, seconds = 192_000, 20
    t = np.arange(rate * seconds, dtype=np.float32) / rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, np.stack([tone, tone], axis=1), rate, format="WAV", subtype="PCM_16")
    data = buf.getvalue()
    expected = soxr.resample(tone, rate, 16000)
    del t, buf

    tracemalloc.start()
    try:
        pcm = routes.decode_upload(data, seconds + 1)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # Decoding it whole would hold 20 s x 192 kHz x 2 channels of float32 (~31 MB) at once.
    assert peak < 12 * 1024 * 1024, peak
    assert pcm.dtype == np.float32 and abs(pcm.size - expected.size) <= 2
    n = min(pcm.size, expected.size)
    assert float(np.max(np.abs(pcm[1000 : n - 1000] - expected[1000 : n - 1000]))) < 0.01


# -- logs, bind address, token file --------------------------------------------------------


def test_tokens_and_tickets_are_redacted_from_access_logs():
    assert cli.redact_secrets("/v1/dictate?token=abc123") == "/v1/dictate?token=***"
    assert cli.redact_secrets("/v1/dictate?profile=en&ticket=x-y_z&b=1") == "/v1/dictate?profile=en&ticket=***&b=1"
    assert cli.redact_secrets("/v1/health") == "/v1/health"
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', (
        "100.64.0.1:5000", "GET", "/v1/meetings/1/stream?token=s3cret", "1.1", 101), None)
    assert cli.RedactSecrets().filter(rec)
    assert "s3cret" not in rec.getMessage() and "token=***" in rec.getMessage()
    ws_rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
                               ("100.64.0.1:5000", "/v1/dictate?ticket=t1"), None)
    cli.RedactSecrets().filter(ws_rec)
    assert ws_rec.getMessage().endswith('"WebSocket /v1/dictate?ticket=***" [accepted]')
    cfg = cli.uvicorn_log_config()
    assert cfg["filters"]["redact_secrets"]["()"] is cli.RedactSecrets
    assert all("redact_secrets" in h["filters"] for h in cfg["handlers"].values())
    from uvicorn.config import LOGGING_CONFIG
    assert "filters" not in LOGGING_CONFIG["handlers"]["access"]  # uvicorn's own dict left alone


def test_forwarded_headers_are_ignored_unless_trusted(tmp_path, monkeypatch):
    """Uvicorn's default trusts X-Forwarded-For from 127.0.0.1: with tailnet_auth any local
    process could then claim to be one of your Tailscale devices."""
    import uvicorn
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\nmodels_dir = "{tmp_path}"\n')
    for k in ("TRANSCRIPTD_DATA_DIR", "TRANSCRIPTD_MODELS_DIR", "TRANSCRIPTD_HOST", "TRANSCRIPTD_PORT"):
        monkeypatch.delenv(k, raising=False)
    runs = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append((app, kw)))
    assert cli.main(["--config", str(cfg), "serve", "--port", "17499"]) == 0
    app, kw = runs[-1]
    assert kw["proxy_headers"] is False and kw["port"] == 17499
    config = uvicorn.Config(app, **kw)
    config.load()
    assert not isinstance(config.loaded_app, ProxyHeadersMiddleware)

    cfg.write_text(cfg.read_text() + "trust_proxy_headers = true\n")
    cli.main(["--config", str(cfg), "serve"])
    assert runs[-1][1]["proxy_headers"] is True


def test_the_server_listens_on_loopback_by_default(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTD_HOST", raising=False)
    assert Settings().host == "127.0.0.1"


def test_the_token_file_is_owner_only_from_the_start(tmp_path):
    old = os.umask(0)  # even with no umask to help
    try:
        tok = _ensure_token(tmp_path)
    finally:
        os.umask(old)
    f = tmp_path / "token"
    assert f.read_text() == tok and stat.S_IMODE(f.stat().st_mode) == 0o600
    f.chmod(0o644)
    assert _ensure_token(tmp_path) == tok
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


# -- what the server leaves on disk ------------------------------------------------------


def test_the_data_dir_database_and_audio_are_owner_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_pipeline import FakeEngine, _w
    from transcriptd.app import create_app

    data = tmp_path / "data"
    data.mkdir(mode=0o755)  # an existing data dir others can read is tightened
    data.chmod(0o755)
    (data / "transcriptd.db").touch()
    (data / "transcriptd.db").chmod(0o644)  # and so is an existing database
    monkeypatch.setenv("TRANSCRIPTD_DATA_DIR", str(data))
    monkeypatch.setenv("TRANSCRIPTD_MODELS_DIR", str(tmp_path / "models"))
    old = os.umask(0o022)
    try:
        s = Settings.load(tmp_path / "nope.toml")
        s.keep_dictation_audio = True
        app = create_app(s)
        with TestClient(app) as c:
            app.state.registry._engines["english"] = FakeEngine(_w("never merge"))
            c.headers["Authorization"] = f"Bearer {s.token}"
            r = c.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"})
            did = r.json()["dictation_id"]
            mid = c.post("/v1/meetings", json={}).json()["id"]
            stereo = io.BytesIO()
            sf.write(stereo, np.zeros((16000, 2), dtype=np.float32), 16000, format="WAV", subtype="PCM_16")
            assert c.post(f"/v1/meetings/{mid}/audio?process=false",
                          files={"file": ("m.wav", stereo.getvalue(), "audio/wav")}).status_code == 200
            def mode(p):
                return stat.S_IMODE(p.stat().st_mode)
            assert mode(data) == 0o700
            for d in ("dictations", "meetings"):
                assert mode(data / d) == 0o700
            files = [data / "transcriptd.db", data / "dictations" / f"{did}.wav", data / "meetings" / f"{mid}.wav",
                     *data.glob("transcriptd.db-*")]
            assert all(f.exists() for f in files[:3])
            assert {str(f.name): mode(f) for f in files} == {str(f.name): 0o600 for f in files}
    finally:
        os.umask(old)


def test_sqlite_overwrites_deleted_rows(tmp_path):
    from sqlalchemy import text

    from transcriptd.db import Database

    db = Database(tmp_path / "t.db")
    for _ in range(2):  # every pooled connection, not just the first
        with db.engine.connect() as con:
            assert con.execute(text("PRAGMA secure_delete")).scalar() == 1
    db.engine.dispose()
