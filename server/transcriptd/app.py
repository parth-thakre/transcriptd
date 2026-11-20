"""FastAPI application factory. Everything shared lives on `app.state`."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .asr.registry import EngineRegistry
from .config import Settings
from .db import Database
from .pipeline import Pipeline

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = Database(settings.data_dir / "transcriptd.db")
        app.state.registry = EngineRegistry(settings)
        app.state.pipeline = Pipeline(settings, app.state.registry, app.state.db)
        app.state.pipeline.reload_vocab()
        log.info("transcriptd ready; default profile %s; data in %s", settings.default_profile, settings.data_dir)
        try:
            yield
        finally:
            app.state.registry.close()

    app = FastAPI(title="transcriptd", version="0.1.0", lifespan=lifespan)

    from .api import dictate, meetings, routes
    from .api.deps import TicketStore

    app.state.ws_tickets = TicketStore()
    app.state.meeting_jobs = meetings.MeetingJobs()

    app.include_router(routes.router)
    app.include_router(dictate.router)
    app.include_router(meetings.router)
    app.include_router(meetings.ws_router)
    # Added before _mount_web's middleware, so it sits inside it: FastAPI then gets the 413 raised
    # from receive() as is (through BaseHTTPMiddleware it would arrive wrapped, as a 400).
    app.add_middleware(BodyLimit, limit_for=lambda path: body_limit(settings, path))
    _mount_web(app)
    return app


_MEETING_AUDIO = re.compile(r"^/v1/meetings/\d+/audio/?$")


def body_limit(settings: Settings, path: str) -> int:
    """Largest request body (bytes) accepted at `path`."""
    mb = settings.max_meeting_upload_mb if _MEETING_AUDIO.match(path) else settings.max_upload_mb
    return int(mb * 1024 * 1024)


class BodyLimit:
    """Refuses request bodies over the limit with 413: up front from Content-Length, or as soon
    as the bytes received pass it, so nothing (not even the multipart parser's spool file) holds
    more than the limit plus one chunk."""

    def __init__(self, app, limit_for: Callable[[str], int]):
        self.app = app
        self.limit_for = limit_for

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > limit:
            await _send_413(send, limit)
            return
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge(limit)
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _TooLarge:  # read outside a route (FastAPI turns it into a 413 inside one)
            if started:
                raise
            await _send_413(send, limit)


class _TooLarge(HTTPException):
    def __init__(self, limit: int):
        super().__init__(413, f"request body larger than {limit / 1024 / 1024:g} MB")


async def _send_413(send, limit: int) -> None:
    body = json.dumps({"detail": _TooLarge(limit).detail}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


WEB_DIR = Path(__file__).parent / "web"


def _mount_web(app: FastAPI) -> None:
    """The phone app (PWA) at /m/. Static and public; every API call it makes carries the token."""
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    mimetypes.add_type("application/manifest+json", ".webmanifest")
    mimetypes.add_type("text/javascript", ".js")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/m/")

    @app.middleware("http")
    async def revalidate_web(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/m/"):
            # Small files on a private network: always revalidate so a server update shows up at once.
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/m", StaticFiles(directory=WEB_DIR, html=True), name="web")
