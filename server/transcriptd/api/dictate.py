"""WS /v1/dictate: push-to-talk session.

Client -> server:
  text  {"type":"start","profile":"english","language":null,"app_hint":"firefox","partials":true,
         "style":"note",   (optional post-cleanup style, see [cleanup.styles])
         "llm":false,      (optional: override [cleanup] llm for this dictation)
         "context":{"app":"firefox","before":"text before the caret","after":"text after"}}
  binary  int16 LE mono 16 kHz PCM frames (any size; 20 ms is typical)
  text  {"type":"end"}   -> server runs the final pass and replies with "final"
  text  {"type":"cancel"}
Auth: the token (Authorization header, or ?token= for older clients), a single-use ?ticket= from
POST /v1/ws-ticket, or tailnet identity (see deps.py). Takes are capped at max_dictation_seconds.
Server -> client:
  {"type":"ready","profile":...}
  {"type":"partial","text":"..."}            (only if the profile has a partials model)
  {"type":"final", ...DictationResult, "audio": {...audio_stats}, "dictation_id": 12}
  {"type":"error","message":"..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..audio import audio_stats, int16_bytes_to_float32
from ..context import DictationContext, profile_for_context
from .deps import ws_reject
from .routes import store_dictation

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/v1/dictate")
async def dictate(ws: WebSocket):
    if (code := await ws_reject(ws)) is not None:
        await ws.close(code=code)
        return
    await ws.accept()
    st = ws.app.state
    max_samples = st.settings.max_dictation_seconds * 16000
    chunks: list[np.ndarray] = []
    total = 0
    profile: str | None = None
    language: str | None = None
    app_hint: str | None = None
    context: DictationContext | None = None
    style: str | None = None
    use_llm: bool | None = None
    partial_stream = None
    partial_task: asyncio.Task | None = None
    last_partial = [""]  # dedupe: only push when the display text changes
    started = False

    async def send(obj: dict) -> None:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                if not started:
                    await send({"type": "error", "message": "send start first"})
                    continue
                pcm = int16_bytes_to_float32(data)
                chunks.append(pcm)
                total += len(pcm)
                if total > max_samples:
                    await send({"type": "error", "message": "utterance too long"})
                    break
                if partial_stream is not None and (partial_task is None or partial_task.done()):
                    partial_task = asyncio.create_task(_partial(partial_stream, pcm, send, last_partial))
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                cmd = json.loads(text)
            except json.JSONDecodeError:
                await send({"type": "error", "message": "bad json"})
                continue
            kind = cmd.get("type")
            if kind == "start":
                profile = cmd.get("profile")
                language = cmd.get("language")
                style = cmd.get("style")
                use_llm = cmd.get("llm")
                context = DictationContext.from_dict(cmd.get("context"))
                app_hint = cmd.get("app_hint") or (context.app if context else None)
                context = DictationContext.with_app(context, app_hint)  # the app decides lists vs chat
                if context is not None and (profile in (None, st.settings.default_profile)):
                    profile = profile_for_context(context, st.settings.default_profile, st.settings.profiles)
                try:
                    name, prof = st.registry.profile(profile)
                except KeyError as exc:
                    await send({"type": "error", "message": str(exc)})
                    continue
                chunks, total, started = [], 0, True
                # A start without an end: drop the old take's partials stream. (Each close below
                # hands the stream over before awaiting: if this handler is cancelled meanwhile,
                # _close_partials still closes it, and the finally mustn't close it again.)
                task, stream, partial_task, partial_stream = partial_task, partial_stream, None, None
                await _close_partials(task, stream)
                if cmd.get("partials") and prof.partials_model:
                    partial_stream = await _open_stream(st, name, language or prof.language)
                await send({"type": "ready", "profile": name})
            elif kind == "end":
                if not started:
                    await send({"type": "error", "message": "not started"})
                    continue
                task, stream, partial_task, partial_stream = partial_task, partial_stream, None, None
                await _close_partials(task, stream)
                pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
                started = False
                if pcm.size < 1600:  # < 100 ms
                    await send({"type": "final", "text": "", "raw_text": "", "words": [], "edits": [],
                                "replacements": [], "low_confidence": [], "dictation_id": None, "empty": True})
                    continue
                try:
                    result = await asyncio.to_thread(st.pipeline.process, pcm, profile=profile, language=language,
                                                    context=context, style=style, use_llm=use_llm)
                except Exception as exc:
                    log.exception("dictation failed")
                    await send({"type": "error", "message": f"transcription failed: {exc}"})
                    continue
                out = result.to_dict()
                out["audio"] = audio_stats(pcm)
                did = await asyncio.to_thread(store_dictation, st, out, app_hint, pcm)
                out.update({"type": "final", "dictation_id": did})
                await send(out)
            elif kind == "cancel":
                chunks, total, started = [], 0, False
                task, stream, partial_task, partial_stream = partial_task, partial_stream, None, None
                await _close_partials(task, stream)
                await send({"type": "cancelled"})
            else:
                await send({"type": "error", "message": f"unknown command {kind!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        await _close_partials(partial_task, partial_stream)


SLOW_FEED_S = 10.0


class _OnceClosed:
    """A take's partials stream that closes at most once. A native close resets the stream and
    releases its engine's lock, so a second close could release the lock under the next take."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self._closed = False

    def feed(self, pcm: np.ndarray):
        return self._stream.feed(pcm)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stream.close()


async def _open_stream(st, profile_name: str, language: str | None) -> _OnceClosed | None:
    """Open a take's partials stream (which takes its engine's lock) in a worker thread. If this
    coroutine is cancelled meanwhile, the stream is closed once it has opened, not left holding
    the lock."""
    fut = asyncio.ensure_future(asyncio.to_thread(_open_partials, st, profile_name, language))
    try:
        opened = await asyncio.shield(fut)
    except asyncio.CancelledError:
        def close_late(f: asyncio.Future) -> None:
            if not f.cancelled() and f.exception() is None and f.result() is not None:
                asyncio.get_running_loop().run_in_executor(None, f.result().close)

        fut.add_done_callback(close_late)
        raise
    return _OnceClosed(opened) if opened is not None else None


async def _close_partials(task: asyncio.Task | None, stream, warn_after: float = SLOW_FEED_S) -> None:
    """Close a take's partials stream (reset it, release its engine's lock) once the feed in
    flight, if any, has returned. A feed is native code in a worker thread: cancelling the task
    wouldn't stop it, and resetting the stream under it, or letting another take have the engine,
    is a use-after-free. So this waits for it however long it takes (warning past `warn_after`),
    and if this coroutine is cancelled meanwhile, the stream is closed when the feed returns."""
    if task is not None and not task.done():
        try:
            done, _ = await asyncio.wait({task}, timeout=warn_after)
            if not done:
                log.warning("a partial transcript is still running after %g s; waiting for it to close its stream",
                            warn_after)
                await asyncio.wait({task})
        except asyncio.CancelledError:
            if stream is not None:
                task.add_done_callback(lambda _t: asyncio.get_running_loop().run_in_executor(None, stream.close))
            raise
    if stream is not None:
        # Submitted to the executor before the first await and shielded: a cancel while the close
        # is still queued behind other work must not drop it, or the engine's lock stays held.
        fut = asyncio.get_running_loop().run_in_executor(None, stream.close)
        fut.add_done_callback(_log_close_failure)
        await asyncio.shield(fut)


def _log_close_failure(fut: asyncio.Future) -> None:
    if not fut.cancelled() and fut.exception() is not None:
        log.error("closing a partials stream failed", exc_info=fut.exception())


async def _in_thread(fn, *args):
    """`fn(*args)` in a worker thread, returning (or, if cancelled, raising CancelledError) only
    once the thread is done: the caller can't be cancelled out from under native code that is
    still using its stream."""
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    cancelled = False
    while not fut.done():
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not fut.cancelled():
            fut.exception()  # retrieved: it's the cancel that propagates
        raise asyncio.CancelledError
    return fut.result()


def _open_partials(st, profile_name: str, language: str | None):
    from ..asr.ggml import GgmlEngine, GgmlPartialStream
    from ..asr.registry import resolve_ggml_model

    prof = st.settings.profiles[profile_name]
    key = f"{profile_name}:partials"
    eng = st.registry._engines.get(key)
    if eng is None:
        path = resolve_ggml_model(prof.partials_model, prof.quant, st.settings.models_dir)
        eng = GgmlEngine(path, backend=prof.backend)
        st.registry._engines[key] = eng
    if not eng.supports_streaming():
        return None
    return GgmlPartialStream(eng, language)


async def _partial(stream, pcm: np.ndarray, send, last: list[str]) -> None:
    try:
        text = await _in_thread(stream.feed, pcm)
        if text and text != last[0]:
            last[0] = text
            await send({"type": "partial", "text": text})
    except Exception as exc:  # partials are best-effort
        log.debug("partial failed: %s", exc)

