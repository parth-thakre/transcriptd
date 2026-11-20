"""Meetings: create, live-stream two channels, upload audio, list/read/export, summarize.

WS /v1/meetings/{id}/stream
  client -> binary: interleaved stereo int16 LE 16 kHz (L = me, R = them), any chunk size
  client -> text  : {"type":"end"}
  server -> text  : {"type":"segment", channel, start_ms, end_ms, text, words, live: true}
                    {"type":"ended", "meeting_id": id}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import select

from ..config import private_dir, private_opener
from ..db import Meeting, MeetingSegment
from ..meetings import MeetingGone, process_meeting, render_transcript, summarize_meeting
from ..segmenter import StreamSegmenter
from ..vad import SileroVAD, ensure_model
from .deps import require_token, ws_reject
from .routes import MB, UPLOAD_CHUNK

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/meetings", dependencies=[Depends(require_token)])
ws_router = APIRouter()

LIVE_QUEUE_SEGMENTS = 32  # live segments waiting for ASR before the socket stops being read


class MeetingJobs:
    """Meeting final passes waiting or running (max_queued_meetings at once, one per meeting)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ids: set[int] = set()

    def claim(self, meeting_id: int, limit: int) -> None:
        with self._lock:
            if meeting_id in self._ids:
                raise HTTPException(409, "this meeting is already being processed")
            if len(self._ids) >= limit:
                raise HTTPException(429, "too many meetings queued for processing; try again later",
                                    headers={"Retry-After": "60"})
            self._ids.add(meeting_id)

    def release(self, meeting_id: int) -> None:
        with self._lock:
            self._ids.discard(meeting_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)


def _jobs(st) -> MeetingJobs:
    jobs = getattr(st, "meeting_jobs", None)
    if jobs is None:
        jobs = st.meeting_jobs = MeetingJobs()
    return jobs


class MeetingIn(BaseModel):
    title: str = ""
    profile: str | None = None


class MeetingPatch(BaseModel):
    title: str | None = None
    notes: str | None = None


def _meeting_dict(m: Meeting, segments: list[MeetingSegment] | None = None) -> dict:
    d = {"id": m.id, "title": m.title, "started_at": m.started_at.isoformat(), "ended_at": m.ended_at.isoformat() if m.ended_at else None,
         "status": m.status, "profile": m.profile, "has_audio": bool(m.audio_path), "summary": m.summary, "notes": m.notes}
    if segments is not None:
        d["segments"] = [
            {"id": s.id, "channel": s.channel, "speaker": s.speaker, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text,
             "words": json.loads(s.words_json), "final": s.final}
            for s in segments
        ]
    return d


@router.post("", status_code=201)
def create_meeting(request: Request, body: MeetingIn):
    st = request.app.state
    m = Meeting(title=body.title, profile=body.profile or st.settings.default_profile)
    with st.db.session() as s:
        s.add(m)
        s.commit()
        s.refresh(m)
        return _meeting_dict(m)


@router.get("")
def list_meetings(request: Request, limit: int = Query(100, le=1000), q: str | None = None):
    with request.app.state.db.session() as s:
        if q:
            ids = {r.meeting_id for r in s.exec(select(MeetingSegment).where(MeetingSegment.text.contains(q))).all()}
            rows = [m for m in s.exec(select(Meeting).order_by(Meeting.started_at.desc())).all() if m.id in ids or q.lower() in m.title.lower()][:limit]
        else:
            rows = s.exec(select(Meeting).order_by(Meeting.started_at.desc()).limit(limit)).all()
        return [_meeting_dict(m) for m in rows]


@router.get("/{meeting_id}")
def get_meeting(request: Request, meeting_id: int):
    with request.app.state.db.session() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            raise HTTPException(404)
        segs = s.exec(select(MeetingSegment).where(MeetingSegment.meeting_id == meeting_id).order_by(MeetingSegment.start_ms)).all()
        return _meeting_dict(m, list(segs))


@router.patch("/{meeting_id}")
def patch_meeting(request: Request, meeting_id: int, body: MeetingPatch):
    with request.app.state.db.session() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            raise HTTPException(404)
        if body.title is not None:
            m.title = body.title
        if body.notes is not None:
            m.notes = body.notes
        s.add(m)
        s.commit()
        s.refresh(m)
        return _meeting_dict(m)


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(request: Request, meeting_id: int):
    folder = request.app.state.settings.data_dir / "meetings"
    with request.app.state.db.session() as s:
        # One write transaction, bulk statements: a live segment (db.insert_if_exists) or a
        # final pass (process_meeting) either commits before this and is deleted with it, or
        # after it and finds no meeting. DELETE ... RETURNING reads audio_path in the same
        # statement that deletes the row, so an upload attaching its audio can't slip in
        # between the read and the delete.
        s.exec(sa_delete(MeetingSegment).where(MeetingSegment.meeting_id == meeting_id))
        rows = s.exec(sa_delete(Meeting).where(Meeting.id == meeting_id).returning(Meeting.audio_path)).all()
        if not rows:
            s.rollback()
            raise HTTPException(404)
        s.commit()
    # Its audio wherever it is: the path it was attached under, the upload's canonical file (an
    # upload that saved but hasn't attached yet), and an upload still being written.
    for p in {rows[0][0], folder / f"{meeting_id}.wav", folder / f".{meeting_id}.wav.part"}:
        if p:
            Path(p).unlink(missing_ok=True)


@router.get("/{meeting_id}/transcript")
def export_transcript(request: Request, meeting_id: int, fmt: str = Query("txt", pattern="^(txt|md)$")):
    with request.app.state.db.session() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            raise HTTPException(404)
        segs = s.exec(select(MeetingSegment).where(MeetingSegment.meeting_id == meeting_id).order_by(MeetingSegment.start_ms)).all()
        head = f"# {m.title or 'Meeting'}\n\n" if fmt == "md" else ""
        if fmt == "md" and m.summary:
            head += m.summary + "\n\n## Transcript\n\n"
        return PlainTextResponse(head + render_transcript(list(segs), fmt))


@router.post("/{meeting_id}/audio")
async def upload_audio(request: Request, meeting_id: int, background: BackgroundTasks, file: Annotated[UploadFile, File()],
                       process: bool = True):
    """Upload the agent's local stereo WAV (backup or non-streamed meeting) and run the final pass.
    413 past max_meeting_upload_mb; with `process`, 429 when max_queued_meetings are already
    waiting and 409 while this meeting's last upload is still being processed."""
    st = request.app.state
    with st.db.session() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            raise HTTPException(404)
    holding = False  # this request holds the meeting's processing slot (until the job takes it)
    if process:
        _jobs(st).claim(meeting_id, st.settings.max_queued_meetings)
        holding = True
    try:
        size = await asyncio.to_thread(_save_upload, file, st.settings.data_dir / "meetings", meeting_id,
                                       int(st.settings.max_meeting_upload_mb * MB))
        path = st.settings.data_dir / "meetings" / f"{meeting_id}.wav"
        if not await asyncio.to_thread(_attach_audio, st.db, meeting_id, path, process):
            path.unlink(missing_ok=True)  # deleted during the upload: don't keep its audio
            raise HTTPException(404)
        if process:
            background.add_task(_process_bg, st, meeting_id)  # releases the slot when done
            holding = False
    finally:
        if holding:
            _jobs(st).release(meeting_id)
    return {"ok": True, "bytes": size, "processing": process}


def _attach_audio(db, meeting_id: int, path, process: bool) -> bool:
    """Point the meeting at its uploaded audio; False if the meeting no longer exists. UPDATE ...
    WHERE id, so a meeting deleted since the upload started is noticed rather than recreated
    or half-updated."""
    values = {"audio_path": str(path)}
    if process:
        values["status"] = "processing"
    with db.session() as s:
        found = s.exec(sa_update(Meeting).where(Meeting.id == meeting_id).values(**values)).rowcount == 1
        if found:
            s.exec(sa_update(Meeting).where(Meeting.id == meeting_id, Meeting.ended_at.is_(None))
                   .values(ended_at=datetime.now(UTC)))
            s.commit()
    return found


def _save_upload(file: UploadFile, folder, meeting_id: int, limit: int) -> int:
    """Copy the upload to <folder>/<id>.wav a chunk at a time, through a temp file so a refused
    or broken upload never replaces the meeting's audio. 413 past `limit` bytes."""
    private_dir(folder)
    tmp = folder / f".{meeting_id}.wav.part"
    size = 0
    try:
        file.file.seek(0)
        with open(tmp, "wb", opener=private_opener) as out:
            while chunk := file.file.read(UPLOAD_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"upload larger than {limit / MB:g} MB")
                out.write(chunk)
        try:
            tmp.replace(folder / f"{meeting_id}.wav")
        except FileNotFoundError:  # the meeting was deleted meanwhile, and its upload with it
            raise HTTPException(404) from None
    finally:
        tmp.unlink(missing_ok=True)
    return size


@router.post("/{meeting_id}/process")
def reprocess(request: Request, meeting_id: int, background: BackgroundTasks):
    st = request.app.state
    with st.db.session() as s:
        m = s.get(Meeting, meeting_id)
        if not m or not m.audio_path:
            raise HTTPException(404, "no audio for this meeting")
    _jobs(st).claim(meeting_id, st.settings.max_queued_meetings)
    background.add_task(_process_bg, st, meeting_id)
    return {"ok": True}


@router.post("/{meeting_id}/summarize")
def summarize(request: Request, meeting_id: int):
    st = request.app.state
    try:
        return {"summary": summarize_meeting(st.db, st.settings, meeting_id)}
    except MeetingGone as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"summary failed: {exc}") from exc


def _process_bg(st, meeting_id: int) -> None:
    try:
        process_meeting(st.pipeline, st.db, st.settings, meeting_id)
    except Exception:
        log.exception("meeting %s processing failed", meeting_id)
    finally:
        _jobs(st).release(meeting_id)


# -- live stream -----------------------------------------------------------------------


@ws_router.websocket("/v1/meetings/{meeting_id}/stream")
async def stream(ws: WebSocket, meeting_id: int):
    if (code := await ws_reject(ws)) is not None:
        await ws.close(code=code)
        return
    st = ws.app.state
    with st.db.session() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            await ws.close(code=4404)
            return
        profile = m.profile or None
    await ws.accept()
    vad_path = await asyncio.to_thread(ensure_model, st.settings.models_dir)
    segs = {ch: StreamSegmenter(SileroVAD(vad_path)) for ch in ("me", "them")}
    # Bounded: when transcription falls behind, reading the socket waits (TCP backpressure)
    # instead of piling up segments in memory.
    queue: asyncio.Queue = asyncio.Queue(maxsize=LIVE_QUEUE_SEGMENTS)
    worker = asyncio.create_task(_segment_worker(st, ws, meeting_id, profile, queue))
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                pcm = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                if len(pcm) % 2:
                    pcm = pcm[:-1]
                me, them = pcm[0::2], pcm[1::2]
                for ch, buf in (("me", me), ("them", them)):
                    for s_ in segs[ch].feed(buf):
                        await queue.put((ch, s_))
                continue
            if (text := msg.get("text")) and json.loads(text).get("type") == "end":
                for ch in ("me", "them"):
                    for s_ in segs[ch].flush():
                        await queue.put((ch, s_))
                await queue.put(None)
                await worker
                with st.db.session() as s:
                    m = s.get(Meeting, meeting_id)
                    if m is not None:  # else deleted while it was streaming
                        m.ended_at = datetime.now(UTC)
                        if m.status == "recording":
                            m.status = "done"  # a later audio upload can upgrade this with the final pass
                        s.add(m)
                        s.commit()
                await ws.send_text(json.dumps({"type": "ended", "meeting_id": meeting_id}))
                break
    except WebSocketDisconnect:
        pass
    finally:
        await _stop_worker(worker, queue)


async def _stop_worker(worker: asyncio.Task, queue: asyncio.Queue, timeout: float = 30) -> None:
    """Let the worker finish the segments already queued (they are stored even if the client
    left), then end it; past `timeout` cancel it. Always awaited, so no task is left orphaned."""

    async def drain() -> None:
        while not worker.done():
            try:
                queue.put_nowait(None)
                break
            except asyncio.QueueFull:
                await asyncio.sleep(0.05)
        await worker

    try:
        await asyncio.wait_for(drain(), timeout=timeout)
    except Exception:  # timed out, or the worker failed
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker


async def _segment_worker(st, ws: WebSocket, meeting_id: int, profile: str | None, queue: asyncio.Queue) -> None:
    gone = False  # the meeting was deleted mid-stream: keep reading the queue, transcribe nothing
    while True:
        item = await queue.get()
        if item is None:
            return
        if gone:
            continue
        ch, seg = item
        try:
            r = await asyncio.to_thread(st.pipeline.process, seg.pcm, profile=profile)
        except Exception as exc:
            log.warning("live segment failed: %s", exc)
            continue
        if not r.text.strip():
            continue
        row = MeetingSegment(meeting_id=meeting_id, channel=ch, speaker=ch, start_ms=seg.start_ms, end_ms=seg.end_ms, text=r.text,
                             words_json=json.dumps([w.to_dict() for w in r.words], ensure_ascii=False), final=False)
        row_id = await asyncio.to_thread(st.db.insert_if_exists, row, Meeting, meeting_id)
        if row_id is None:
            gone = True
            continue
        try:
            await ws.send_text(json.dumps({"type": "segment", "id": row_id, "channel": ch, "speaker": ch, "start_ms": seg.start_ms,
                                           "end_ms": seg.end_ms, "text": r.text, "raw_text": r.raw_text, "live": True}, ensure_ascii=False))
        except Exception:
            return
