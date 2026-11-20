"""Meeting processing: segment each channel, transcribe through the pipeline, merge by
time, optionally diarize the remote side and summarize."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from sqlalchemy import delete, update
from sqlmodel import select

from . import diarize, llm
from .audio import SAMPLE_RATE, resample
from .db import Meeting, MeetingSegment
from .pipeline import Pipeline
from .segmenter import StreamSegmenter
from .vad import SileroVAD, ensure_model

log = logging.getLogger(__name__)

CHANNELS = ("me", "them")


class MeetingGone(LookupError):
    """The meeting doesn't exist (any more)."""


def load_stereo(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    me = resample(data[:, 0], rate)
    them = resample(data[:, 1], rate) if data.shape[1] > 1 else np.zeros_like(me)
    return me, them


def transcribe_channel(pipeline: Pipeline, vad: SileroVAD, pcm: np.ndarray, channel: str, profile: str | None,
                       on_segment=None) -> list[dict]:
    seg = StreamSegmenter(vad)
    out = []
    for s in seg.feed(pcm) + seg.flush():
        r = pipeline.process(s.pcm, profile=profile)
        if not r.text.strip():
            continue
        d = {"channel": channel, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": r.text, "raw_text": r.raw_text,
             "words": [w.to_dict() for w in r.words], "language": r.language}
        out.append(d)
        if on_segment:
            on_segment(d)
    return out


def process_meeting(pipeline: Pipeline, db, settings, meeting_id: int) -> None:
    """Offline re-pass over the saved WAV. Replaces live segments with final ones."""
    with db.session() as s:
        m = s.get(Meeting, meeting_id)
        if not m or not m.audio_path:
            raise ValueError(f"meeting {meeting_id} has no audio")
        audio_path, profile, title = m.audio_path, m.profile or None, m.title
        m.status = "processing"
        s.add(m)
        s.commit()
    try:
        vad = SileroVAD(ensure_model(settings.models_dir))
        me, them = load_stereo(audio_path)
        segs = transcribe_channel(pipeline, vad, me, "me", profile) + transcribe_channel(pipeline, vad, them, "them", profile)
        turns = diarize.diarize(them) if them.any() else []
        for d in segs:
            if d["channel"] == "them" and turns:
                d["speaker"] = diarize.speaker_at(turns, d["start_ms"], d["end_ms"]) or "them"
            else:
                d["speaker"] = "me" if d["channel"] == "me" else "them"
        segs.sort(key=lambda d: d["start_ms"])
        with db.session() as s:
            # The meeting row first: from this write to the commit SQLite holds the write lock, so
            # a delete_meeting lands wholly before (nothing is stored) or after (it removes these).
            if s.exec(update(Meeting).where(Meeting.id == meeting_id).values(status="done")).rowcount != 1:
                log.info("meeting %s was deleted while it was processed", meeting_id)
                return
            s.exec(update(Meeting).where(Meeting.id == meeting_id, Meeting.ended_at.is_(None))
                   .values(ended_at=datetime.now(UTC)))
            s.exec(delete(MeetingSegment).where(MeetingSegment.meeting_id == meeting_id))
            for d in segs:
                s.add(MeetingSegment(meeting_id=meeting_id, channel=d["channel"], speaker=d["speaker"], start_ms=d["start_ms"],
                                     end_ms=d["end_ms"], text=d["text"], words_json=json.dumps(d["words"], ensure_ascii=False), final=True))
            s.commit()
        if llm.configured(settings.llm):
            try:
                summarize_meeting(db, settings, meeting_id)
            except Exception as exc:
                log.warning("summary failed for meeting %s: %s", meeting_id, exc)
    except Exception:
        with db.session() as s:
            s.exec(update(Meeting).where(Meeting.id == meeting_id).values(status="failed"))  # if still there
            s.commit()
        raise


def render_transcript(segments: list[MeetingSegment], fmt: str = "txt") -> str:
    lines = []
    for seg in segments:
        t = seg.start_ms // 1000
        stamp = f"{t // 3600:02}:{t % 3600 // 60:02}:{t % 60:02}"
        who = seg.speaker or seg.channel
        if fmt == "md":
            lines.append(f"**[{stamp}] {who}:** {seg.text}")
        else:
            lines.append(f"[{stamp}] {who}: {seg.text}")
    return "\n".join(lines) + "\n"


def summarize_meeting(db, settings, meeting_id: int) -> str:
    """MeetingGone if the meeting doesn't exist, or was deleted while the summary was written."""
    with db.session() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            raise MeetingGone(f"no meeting {meeting_id}")
        segs = s.exec(select(MeetingSegment).where(MeetingSegment.meeting_id == meeting_id).order_by(MeetingSegment.start_ms)).all()
        text = render_transcript(list(segs))
        title = m.title
    summary = llm.summarize(settings.llm, text, title)
    with db.session() as s:
        if s.exec(update(Meeting).where(Meeting.id == meeting_id).values(summary=summary)).rowcount != 1:
            raise MeetingGone(f"meeting {meeting_id} was deleted")
        s.commit()
    return summary
