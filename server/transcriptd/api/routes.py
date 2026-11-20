"""REST endpoints: health, file transcription, vocabulary, corrections, dictation history."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import select

from ..audio import SAMPLE_RATE, audio_stats, load_audio
from ..config import private_dir, private_opener
from ..context import DictationContext, profile_for_context
from ..db import ClientReport, Correction, Dictation, DictationEdit, SuppressedSuggestion, VocabTerm
from ..guard import PROTECTED
from ..text import normalize_key
from ..vocab.matcher import COMMON_WORDS
from .deps import TICKET_TTL_S, require_token, tickets

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


# -- health -----------------------------------------------------------------------------


@router.get("/health")
def health(request: Request):
    st = request.app.state
    return {
        "ok": True,
        "version": request.app.version,
        "default_profile": st.settings.default_profile,
        "profiles": {n: p.model_dump(exclude={"api_key"}) for n, p in st.settings.profiles.items()},
        "loaded": st.registry.loaded(),
        "vocab_terms": len(st.pipeline.matcher().entries),
        "llm_configured": bool(st.settings.llm.base_url),
        "llm_cleanup": bool(st.settings.cleanup.llm and st.settings.llm.base_url),
        "styles": [{"name": n, "label": c.label} for n, c in st.settings.cleanup.styles.items()],
    }


@router.post("/ws-ticket")
def ws_ticket(request: Request):
    """A single-use ticket for one WebSocket (`?ticket=`), good for TICKET_TTL_S seconds, so a
    browser, which can't set headers on a WebSocket, doesn't put the token in the URL."""
    return {"ticket": tickets(request.app).issue(), "expires_in": int(TICKET_TTL_S)}


# -- one-shot transcription -------------------------------------------------------------


@router.post("/transcribe")
async def transcribe_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    profile: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    save: Annotated[bool, Form()] = False,
    style: Annotated[str | None, Form()] = None,
    llm: Annotated[bool | None, Form()] = None,
    app_hint: Annotated[str | None, Form()] = None,
    context: Annotated[str | None, Form()] = None,  # JSON, as in the /v1/dictate start message
):
    st = request.app.state
    try:
        ctx = DictationContext.from_dict(json.loads(context)) if context else None
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, f"bad context: {exc}") from exc
    ctx = DictationContext.with_app(ctx, app_hint)  # the app decides lists vs chat
    if ctx is not None and profile in (None, st.settings.default_profile):
        profile = profile_for_context(ctx, st.settings.default_profile, st.settings.profiles)
    data = await read_upload(file, int(st.settings.max_upload_mb * MB))
    try:
        pcm = await asyncio.to_thread(decode_upload, data, st.settings.max_dictation_seconds)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"could not decode audio: {exc}") from exc
    try:
        result = await asyncio.to_thread(st.pipeline.process, pcm, profile=profile, language=language, style=style,
                                         use_llm=llm, context=ctx)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    out = result.to_dict()
    out["audio"] = audio_stats(pcm)
    if save:
        out["dictation_id"] = await asyncio.to_thread(store_dictation, st, out, app_hint or (ctx.app if ctx else None), pcm)
    return out


MB = 1024 * 1024
UPLOAD_CHUNK = 1024 * 1024
MAX_UPLOAD_RATE = 192_000  # Hz
DECODE_BLOCK_BYTES = 4 * MB


def decode_upload(data: bytes, max_seconds: float) -> np.ndarray:
    """An uploaded file (WAV/FLAC/OGG) as float32 mono 16 kHz, like audio.load_audio, but 413
    past `max_seconds`: a few MB of FLAC can hold hours of audio. The header's length is checked
    first, and decoding stops one second past the limit whatever the header says. It decodes
    about 4 MB at a time, mixed down to mono and resampled as it goes, so memory is the 16 kHz
    result whatever the source's rate and channels (600 s at 192 kHz would be ~460 MB)."""
    import soundfile as sf
    import soxr

    with sf.SoundFile(io.BytesIO(data)) as f:
        rate = f.samplerate
        if not 0 < rate <= MAX_UPLOAD_RATE:
            raise HTTPException(400, f"unsupported sample rate {rate}")
        limit = int(max_seconds * rate)
        too_long = HTTPException(413, f"audio longer than {max_seconds:g} s")
        if f.frames > limit:
            raise too_long
        step = max(1, min(rate, DECODE_BLOCK_BYTES // (4 * f.channels)))
        stream = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32") if rate != SAMPLE_RATE else None
        blocks, total = [], 0
        while total <= limit and len(block := f.read(step, dtype="float32", always_2d=True)):
            mono = block.mean(axis=1, dtype=np.float32) if block.shape[1] > 1 else block[:, 0]
            blocks.append(stream.resample_chunk(mono) if stream else mono.copy())
            total += len(block)
    if total > limit:
        raise too_long
    if stream:
        blocks.append(stream.resample_chunk(np.zeros(0, dtype=np.float32), last=True))
    return np.concatenate(blocks).astype(np.float32, copy=False) if blocks else np.zeros(0, dtype=np.float32)


async def read_upload(file: UploadFile, limit: int) -> bytes:
    """The upload's bytes, read a chunk at a time; 413 as soon as it passes `limit`. (The body
    limit in app.py has already stopped anything much bigger from being received at all.)"""
    out = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK):
        out += chunk
        if len(out) > limit:
            raise HTTPException(413, f"upload larger than {limit / MB:g} MB")
    return bytes(out)


def store_dictation(st, out: dict, app_hint: str | None, pcm: np.ndarray | None = None) -> int:
    """Save a result (`DictationResult.to_dict()` plus "audio"), and with keep_dictation_audio the
    take itself as data/dictations/<id>.wav. Nothing prunes either; deleting the dictation does."""
    d = Dictation(profile=out["profile"], language=out["language"], raw_text=out["raw_text"], text=out["text"],
                  result_json=json.dumps(out, ensure_ascii=False), duration_ms=out["duration_ms"], app_hint=app_hint)
    with st.db.session() as s:
        s.add(d)
        s.commit()
        s.refresh(d)
        did = d.id
    if pcm is not None and st.settings.keep_dictation_audio:
        import soundfile as sf

        # The row is visible (and deletable) from here on. delete_dictation always removes
        # dictations/<id>.wav, and the path is attached with UPDATE ... WHERE id: a dictation
        # deleted before or while the file was written takes its audio with it.
        path = private_dir(st.settings.data_dir / "dictations") / f"{did}.wav"
        try:
            with open(path, "wb", opener=private_opener) as fh:
                sf.write(fh, pcm, 16000, format="WAV", subtype="PCM_16")
        except BaseException:  # e.g. ENOSPC: no half-written WAV left behind (the row may be gone)
            path.unlink(missing_ok=True)
            raise
        with st.db.session() as s:
            found = s.exec(sa_update(Dictation).where(Dictation.id == did).values(audio_path=str(path))).rowcount == 1
            s.commit()
        if not found:
            path.unlink(missing_ok=True)
    return did


# -- dictation history ------------------------------------------------------------------


@router.get("/dictations")
def list_dictations(request: Request, limit: int = Query(50, le=500), q: str | None = None):
    with request.app.state.db.session() as s:
        stmt = select(Dictation).order_by(Dictation.created_at.desc()).limit(limit)
        if q:
            stmt = select(Dictation).where(Dictation.text.contains(q)).order_by(Dictation.created_at.desc()).limit(limit)
        rows = s.exec(stmt).all()
        return [
            {"id": d.id, "created_at": d.created_at.isoformat(), "profile": d.profile, "language": d.language,
             "raw_text": d.raw_text, "text": d.text, "duration_ms": d.duration_ms, "app_hint": d.app_hint}
            for d in rows
        ]


@router.get("/dictations/{dictation_id}")
def get_dictation(request: Request, dictation_id: int):
    with request.app.state.db.session() as s:
        d = s.get(Dictation, dictation_id)
        if not d:
            raise HTTPException(404)
        return {"id": d.id, "created_at": d.created_at.isoformat(), "result": json.loads(d.result_json)}


class RestyleIn(BaseModel):
    style: str
    llm: bool | None = None  # override [cleanup] llm


@router.post("/dictations/{dictation_id}/restyle")
async def restyle_dictation(request: Request, dictation_id: int, body: RestyleIn):
    """Re-run cleanup on a saved dictation's raw words with another style (no new ASR pass)."""
    st = request.app.state
    with st.db.session() as s:
        d = s.get(Dictation, dictation_id)
        if not d:
            raise HTTPException(404)
        stored, app = json.loads(d.result_json), d.app_hint
    result = await asyncio.to_thread(st.pipeline.restyle, stored, body.style, body.llm, app)
    out = result.to_dict()
    out["timings_ms"] = {**stored.get("timings_ms", {}), **out["timings_ms"]}
    with st.db.session() as s:
        d = s.get(Dictation, dictation_id)
        if not d:  # deleted while the cleanup ran
            raise HTTPException(404)
        d.text = result.text
        d.result_json = json.dumps(out, ensure_ascii=False)
        s.add(d)
        s.commit()
    return {**out, "dictation_id": dictation_id}


@router.delete("/dictations/{dictation_id}", status_code=204)
def delete_dictation(request: Request, dictation_id: int):
    st = request.app.state
    with st.db.session() as s:
        # One write transaction, bulk statements: an edit stored by dictation_edited
        # (db.insert_if_exists) either commits before it and is deleted here, or runs after and
        # finds no dictation. RETURNING reads audio_path in the statement that deletes the row.
        s.exec(sa_delete(DictationEdit).where(DictationEdit.dictation_id == dictation_id))  # the field's text
        s.exec(sa_update(Correction).where(Correction.dictation_id == dictation_id)
               .values(dictation_id=None))  # keep what the user taught us
        s.exec(sa_update(ClientReport).where(ClientReport.dictation_id == dictation_id).values(dictation_id=None))
        rows = s.exec(sa_delete(Dictation).where(Dictation.id == dictation_id).returning(Dictation.audio_path)).all()
        if not rows:
            s.rollback()
            raise HTTPException(404)
        s.commit()
    # The canonical path too: store_dictation may be writing it and not have attached it yet.
    for p in {rows[0][0], st.settings.data_dir / "dictations" / f"{dictation_id}.wav"} - {None}:
        Path(p).unlink(missing_ok=True)
    st.pipeline.forget_learned()  # its raw words and edits may be cached as LLM examples


# -- vocabulary -------------------------------------------------------------------------


class VocabIn(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    heard_as: list[str] = Field(default_factory=list)
    language: str | None = None
    added_from: str = "manual"


@router.get("/vocab")
def list_vocab(request: Request):
    return [t.to_dict() for t in request.app.state.db.vocab_terms()]


def _upsert_vocab(s, body: VocabIn) -> dict:
    """Add `body` as a vocab term, or merge its aliases and heard_as into the term with the same
    normalized key, in session `s` (uncommitted; a `write_session`, so no other writer runs
    between the lookup and the write)."""
    key = normalize_key(body.term)
    t = next((t for t in s.exec(select(VocabTerm)).all() if normalize_key(t.term) == key), None)
    if t:
        t.heard_as = sorted(set(t.heard_as) | {normalize_key(h) for h in body.heard_as if h})
        t.aliases = sorted(set(t.aliases) | {a for a in body.aliases if a})
    else:
        t = VocabTerm(term=body.term.strip(), language=body.language, added_from=body.added_from)
        t.aliases = [a for a in body.aliases if a]
        t.heard_as = sorted({normalize_key(h) for h in body.heard_as if h})
    s.add(t)
    s.flush()
    s.refresh(t)
    return t.to_dict()


@router.post("/vocab", status_code=201)
def add_vocab(request: Request, body: VocabIn):
    st = request.app.state
    with st.db.write_session() as s:
        out = _upsert_vocab(s, body)
        s.commit()
    st.pipeline.reload_vocab()
    return out


@router.put("/vocab/{term_id}")
def update_vocab(request: Request, term_id: int, body: VocabIn):
    st = request.app.state
    # One write transaction from the read: a delete lands wholly before (404) or after it.
    with st.db.write_session() as s:
        t = s.get(VocabTerm, term_id)
        if not t:
            raise HTTPException(404)
        t.term = body.term.strip()
        t.aliases = [a for a in body.aliases if a]
        t.heard_as = [normalize_key(h) for h in body.heard_as if h]
        t.language = body.language
        s.add(t)
        s.flush()
        out = t.to_dict()  # before the commit: a delete right after it can't make this a 500
        s.commit()
    st.pipeline.reload_vocab()
    return out


@router.delete("/vocab/{term_id}", status_code=204)
def delete_vocab(request: Request, term_id: int):
    st = request.app.state
    # One write transaction from the first read: a correction can't land between the read of
    # the corrections and their delete, and record_correction (also one) runs wholly before or after.
    with st.db.write_session() as s:
        t = s.get(VocabTerm, term_id)
        if not t:
            raise HTTPException(404)
        # The corrections that taught the term go with it, in the same transaction, or the next
        # correction for the same word would teach it back.
        key = normalize_key(t.term)
        stale = [c.id for c in s.exec(select(Correction)).all() if normalize_key(c.meant) == key]
        if stale:
            s.exec(sa_delete(Correction).where(Correction.id.in_(stale)))
        s.delete(t)
        s.commit()
    st.pipeline.reload_vocab()


@router.get("/vocab/export", response_model=None)
def export_vocab(request: Request):
    """Plain text: one term per line, optional `| alias, alias | heard as, heard as`."""
    lines = []
    for t in request.app.state.db.vocab_terms():
        line = t.term
        if t.aliases or t.heard_as:
            line += " | " + ", ".join(t.aliases)
        if t.heard_as:
            line += " | " + ", ".join(t.heard_as)
        lines.append(line)
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("\n".join(lines) + "\n")


class VocabImport(BaseModel):
    text: str


@router.post("/vocab/import")
def import_vocab(request: Request, body: VocabImport):
    added = 0
    for line in body.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        term = parts[0]
        aliases = [a.strip() for a in parts[1].split(",")] if len(parts) > 1 and parts[1] else []
        heard = [h.strip() for h in parts[2].split(",")] if len(parts) > 2 and parts[2] else []
        add_vocab(request, VocabIn(term=term, aliases=aliases, heard_as=heard, added_from="import"))
        added += 1
    return {"added": added}


class SuppressIn(BaseModel):
    word: str


@router.post("/vocab/suppress", status_code=204)
def suppress_suggestion(request: Request, body: SuppressIn):
    st = request.app.state
    with st.db.session() as s:
        s.add(SuppressedSuggestion(key=normalize_key(body.word)))
        s.commit()
    st.pipeline.reload_vocab()


# -- corrections (the loop) -------------------------------------------------------------


class CorrectionIn(BaseModel):
    heard: str
    meant: str
    dictation_id: int | None = None
    source: str = "overlay_edit"


AUTO_LEARN_AFTER = 2  # distinct takes in which the same word was corrected to before it becomes vocabulary


@router.post("/corrections")
def add_correction(request: Request, body: CorrectionIn):
    return record_correction(request, body)


def record_correction(request: Request, body: CorrectionIn) -> dict:
    """Record that the user changed `heard` to `meant`.

    - term already in vocab: silently learn `heard` as a new misrecognition of it
    - same target corrected in AUTO_LEARN_AFTER distinct takes: add it to vocab automatically
      (corrections from one dictation count once, however often it sends them; corrections with
      no dictation count one each)
    - otherwise: readback corrections are just recorded; explicit ones (overlay edit,
      selection hotkey) may prompt the client to offer adding the term
    """
    st = request.app.state
    heard_k, meant_k = normalize_key(body.heard), normalize_key(body.meant)
    if not meant_k or heard_k == meant_k:
        return {"suggest": False, "reason": "no change"}
    if meant_k in PROTECTED or heard_k in PROTECTED:
        return {"suggest": False, "reason": "protected word"}
    # The whole decision (record, read the history and the vocab, learn) is one write
    # transaction, so a vocab delete (the term and its corrections, also one) lands wholly before
    # or after it: never between the history read and the learn, where stale history would bring
    # the deleted term back.
    with st.db.write_session() as s:
        rows = s.exec(select(Correction)).all()
        repeat = body.dictation_id is not None and any(
            c.dictation_id == body.dictation_id and normalize_key(c.heard) == heard_k and normalize_key(c.meant) == meant_k
            for c in rows)
        if not repeat:  # the same fix sent again for the same take is already on record
            s.add(Correction(dictation_id=body.dictation_id, heard=body.heard, meant=body.meant, source=body.source))
            s.flush()
            rows = s.exec(select(Correction)).all()
        known = next((t.id for t in s.exec(select(VocabTerm)).all() if normalize_key(t.term) == meant_k), None)
        suppressed = s.exec(select(SuppressedSuggestion).where(SuppressedSuggestion.key == meant_k)).first() is not None
        # Common-word fixes are kept as history but never count toward learning.
        prior = [c for c in rows if normalize_key(c.meant) == meant_k and normalize_key(c.heard) not in COMMON_WORDS]
        takes = len({("take", c.dictation_id) if c.dictation_id is not None else ("row", c.id) for c in prior})
        if suppressed:
            out = {"suggest": False, "reason": "suppressed"}
        elif heard_k in COMMON_WORDS:  # "to" -> "two" is a one-off fix; learning it would rewrite every "to"
            out = {"suggest": False, "reason": "common word"}
        elif known is not None:
            # Into the term as it is now, matched by id and key; never recreated.
            if st.db.add_heard_as(known, meant_k, heard_k, s):
                out = {"suggest": False, "reason": "learned heard_as", "term": body.meant, "heard_as": body.heard,
                       "learned": True}
            else:
                out = {"suggest": False, "reason": "term deleted", "term": body.meant}
        elif takes >= AUTO_LEARN_AFTER:
            heards = sorted({normalize_key(c.heard) for c in prior if normalize_key(c.heard)})
            _upsert_vocab(s, VocabIn(term=body.meant, heard_as=heards, added_from=body.source))
            out = {"suggest": False, "reason": "auto-learned", "term": body.meant, "heard_as": body.heard,
                   "learned": True, "times_seen": takes}
        elif body.source == "readback":
            out = {"suggest": False, "reason": "recorded", "term": body.meant, "times_seen": takes}
        else:
            out = {"suggest": True, "term": body.meant, "heard_as": body.heard, "times_seen": takes}
        s.commit()
    if out.get("learned"):
        st.pipeline.reload_vocab()
    return out


class EditedIn(BaseModel):
    """The text field after the user was done with an inserted dictation (sent, moved on, or a
    minute later): a window of the field around the dictation (the client trims it to 4000
    characters), and what the client inserted. `final_text` null means the field
    still held exactly what was inserted."""

    final_text: str | None = Field(default=None, max_length=4000)
    inserted_text: str | None = None
    app: str | None = None
    reason: str | None = None


@router.post("/dictations/{dictation_id}/edited")
def dictation_edited(request: Request, dictation_id: int, body: EditedIn):
    """Learn from what the user did to a dictation: words they swapped go to the corrections loop
    (misheard words become vocabulary); the edit is kept, and recent edits steer the LLM pass
    (learn.preferences, learn.examples). With `final_text` null the dictation went out as
    inserted: that is recorded as an unchanged edit (no field text kept), with nothing to learn."""
    from ..learn import Diff, diff, locate

    st = request.app.state
    with st.db.session() as s:
        d = s.get(Dictation, dictation_id)
        if not d:
            raise HTTPException(404)
        stored = json.loads(d.result_json or "{}")
    typed = body.inserted_text or stored.get("insert_text") or d.text
    if body.final_text is None:
        edited, changes = typed, Diff()
    else:
        edited = locate(typed, body.final_text)
        changes = diff(typed, edited)
    row = DictationEdit(dictation_id=dictation_id, app=body.app or d.app_hint, reason=body.reason,
                        inserted_text=typed, final_text=body.final_text or "", edited_text=edited,
                        changed=changes.changed)
    # The dictation is checked again in the same statement as the insert: it may have been
    # deleted since we read it, and the edit (the field's text) must not outlive it.
    if st.db.insert_if_exists(row, Dictation, dictation_id) is None:
        raise HTTPException(404)
    learned = []
    for heard, meant in changes.swaps:
        r = record_correction(request, CorrectionIn(heard=heard, meant=meant, dictation_id=dictation_id,
                                                    source="readback"))
        learned.append({"heard": heard, "meant": meant, **r})
    st.pipeline.forget_learned()
    return {"changed": changes.changed, "edited_text": edited, "swaps": learned,
            "deleted": changes.deleted, "added": changes.added}


class DictationFinalIn(BaseModel):
    """The client tells us what it actually injected (after overlay edits)."""

    final_text: str


@router.post("/dictations/{dictation_id}/final")
def set_final_text(request: Request, dictation_id: int, body: DictationFinalIn):
    with request.app.state.db.session() as s:
        d = s.get(Dictation, dictation_id)
        if not d:
            raise HTTPException(404)
        d.text = body.final_text
        s.add(d)
        s.commit()
    return {"ok": True}


# -- telemetry --------------------------------------------------------------------------

MAX_REPORT_BYTES = 64_000


@router.post("/telemetry", status_code=201)
async def add_report(request: Request):
    """A client's report on one take (any JSON object): device, microphone, timings, how the
    text went in, and in the playground the sentence that was read out ("expected").
    `dictation_id` links it to the server's side of the same take, if it got that far."""
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_REPORT_BYTES:
            raise HTTPException(413, "report too large")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, f"bad json: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "expected a JSON object")
    did = data.get("dictation_id")
    r = ClientReport(dictation_id=did if isinstance(did, int) else None, client=str(data.get("client") or "")[:40],
                     data_json=json.dumps(data, ensure_ascii=False))
    with request.app.state.db.session() as s:
        s.add(r)
        s.commit()
        s.refresh(r)
        return {"id": r.id}


@router.get("/telemetry")
def list_reports(request: Request, limit: int = Query(100, le=2000)):
    """Reports, newest first, each with its dictation: what was heard, the audio stats, timings."""
    return reports(request.app.state.db, limit)


def reports(db, limit: int) -> list[dict]:
    with db.session() as s:
        rows = s.exec(select(ClientReport).order_by(ClientReport.created_at.desc()).limit(limit)).all()
        out = []
        for r in rows:
            d = s.get(Dictation, r.dictation_id) if r.dictation_id else None
            res = json.loads(d.result_json) if d else {}
            if d and "audio" not in res and d.audio_path and Path(d.audio_path).exists():
                res["audio"] = audio_stats(load_audio(d.audio_path)[0])  # kept before stats were
            out.append({
                "id": r.id, "created_at": r.created_at.isoformat(), "client": r.client, "dictation_id": r.dictation_id,
                "report": json.loads(r.data_json),
                "dictation": d and {
                    "profile": d.profile, "raw_text": d.raw_text, "text": d.text, "duration_ms": d.duration_ms,
                    "app_hint": d.app_hint, "audio_path": d.audio_path, "audio": res.get("audio"),
                    "timings_ms": res.get("timings_ms"), "style": res.get("style"),
                },
            })
        return out
