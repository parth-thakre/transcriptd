"""Takes longer than the model's window, for any engine.

Whisper decodes 30 s windows and makes things up over a silent last one: a 30.26 s phone take
ended "... the Landmark Thanks for watching!", "Thanks" at 30.0 s, over the 0.26 s after the
window. Engines without long-form decoding (chat_audio, the mixed-code HF path) cut fixed 30 s
windows, mid-word. So a take longer than the engine's `window_ms` (missing: 30 s; None: no
window) goes through the Silero VAD first:

- What the VAD calls non-speech after the last speech span is dropped (with `EDGE_PAD_MS`), so
  no window is left with only silence or a click in it. The head is kept: Silero missed a quiet
  "Hello Mic" at the start of a test take.
- An engine that stitches windows itself (`long_form = True`: transcribe.cpp's Whisper) then gets
  the rest whole. Others get it cut at VAD pauses into the fewest pieces that fit the window.
  Cutting for Whisper too was measured and dropped more words at the cuts than it saved.

If the VAD hears no speech the take goes in whole: it misses some quiet speech, and the ASR's
words are the source of truth. Takes that fit the window are never touched.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import numpy as np

from ..audio import SAMPLE_RATE
from .base import Engine, TranscribeRequest, Transcript, Word

log = logging.getLogger(__name__)

DEFAULT_WINDOW_MS = 30_000  # for engines that do not say
HEADROOM_MS = 500  # pieces stay this far under the window (engines pad the audio a little)
EDGE_PAD_MS = 500  # audio kept after the last VAD span
MIN_GAP_MS = 300  # a pause narrower than this is a cut point only if there is no wider one


def window_ms(engine: Engine) -> int | None:
    return getattr(engine, "window_ms", DEFAULT_WINDOW_MS)


def needs_vad(engine: Engine, n_samples: int) -> bool:
    win = window_ms(engine)
    return win is not None and n_samples * 1000 / SAMPLE_RATE > win


def plan_chunks(
    spans: list[tuple[int, int]],
    total_ms: int,
    max_ms: int | None,
    *,
    edge_pad_ms: int = EDGE_PAD_MS,
) -> list[tuple[int, int]]:
    """(start_ms, end_ms) pieces of a take, none longer than `max_ms` (None: no limit).

    `spans` are VAD speech spans in ms. The take is kept from 0 to `edge_pad_ms` after the last
    span and split into the fewest pieces that fit. Each cut is in the middle of a pause: of the
    pauses that leave the rest still fitting, the one nearest the even split point. Speech with
    no pause for `max_ms` is cut hard. No spans: the whole take."""
    if total_ms <= 0:
        return []
    spans = sorted((max(0, a), min(total_ms, b)) for a, b in spans if b > a)
    if not spans:
        return [(0, total_ms)]
    end = min(total_ms, spans[-1][1] + edge_pad_ms)
    gaps = [((a_end + b_start) // 2, b_start - a_end) for (_, a_end), (b_start, _) in pairwise(spans)
            if b_start > a_end]
    chunks: list[tuple[int, int]] = []
    s = 0
    while max_ms is not None and end - s > max_ms:
        left = math.ceil((end - s) / max_ms)  # pieces still needed, this one included
        ideal = s + (end - s) / left
        lo = max(s + 1, end - (left - 1) * max_ms)  # the rest must fit in left-1 pieces
        hi = s + max_ms
        ok = [(c, w) for c, w in gaps if lo <= c <= hi]
        wide = [(c, w) for c, w in ok if w >= MIN_GAP_MS] or ok
        cut = min(wide, key=lambda cw: abs(cw[0] - ideal))[0] if wide else int(min(hi, max(lo, ideal)))
        chunks.append((s, cut))
        s = cut
    chunks.append((s, end))
    return chunks


def transcribe_chunks(engine: Engine, req: TranscribeRequest, chunks: list[tuple[int, int]]) -> Transcript:
    """Run the engine on each piece of `req.pcm` and join the words, offset to the take's clock."""
    words: list[Word] = []
    timings: dict[str, float] = {}
    members: dict[str, list[str]] = {}
    first: Transcript | None = None
    for a, b in chunks:
        t = engine.transcribe(replace(req, pcm=req.pcm[a * SAMPLE_RATE // 1000 : b * SAMPLE_RATE // 1000]))
        words.extend(Word(w.text, w.start_ms + a, w.end_ms + a, w.conf) for w in t.words)
        for k, v in t.timings_ms.items():
            timings[k] = round(timings.get(k, 0.0) + v, 1)
        for k, v in t.members.items():
            members.setdefault(k, []).append(v)
        if first is None:
            first = t
        elif first.language is None:
            first.language = t.language
    assert first is not None
    timings["chunks"] = len(chunks)
    return Transcript(words=words, language=first.language, scored=first.scored, engine=first.engine,
                      model=first.model, timings_ms=timings,
                      members={k: " ".join(x for x in v if x) for k, v in members.items()})


def transcribe_long(engine: Engine, req: TranscribeRequest, spans: list[tuple[int, int]]) -> Transcript:
    """A take longer than the engine's window, given the VAD's speech spans over `req.pcm`."""
    total_ms = int(len(req.pcm) * 1000 / SAMPLE_RATE)
    win = window_ms(engine) or DEFAULT_WINDOW_MS
    max_ms = None if getattr(engine, "long_form", False) else max(win - HEADROOM_MS, 1_000)
    chunks = plan_chunks(spans, total_ms, max_ms)
    if chunks == [(0, total_ms)]:
        return engine.transcribe(req)
    return transcribe_chunks(engine, req, chunks)


class SpeechSpans:
    """Silero VAD speech spans for a whole take, loaded on first use and shared between threads.
    Returns None when the VAD cannot be loaded (the take then goes to the engine whole)."""

    def __init__(self, models_dir: Path):
        self.models_dir = models_dir
        self._vad = None
        self._failed = False
        self._lock = threading.Lock()

    def __call__(self, pcm: np.ndarray) -> list[tuple[int, int]] | None:
        with self._lock:
            if self._vad is None and not self._failed:
                try:
                    from ..vad import SileroVAD, ensure_model

                    self._vad = SileroVAD(ensure_model(self.models_dir))
                except Exception as exc:  # no VAD: the engine gets the take whole, as before
                    log.warning("VAD unavailable, long takes go to the engine whole: %s", exc)
                    self._failed = True
            if self._vad is None:
                return None
            # No minimum span length: a short last word ("no", ~200 ms) after a pause is speech
            # the tail cut must keep. Measured on 39 takes past 30 s, the last span ends at the
            # same place either way; the click after take 76's speech never reaches the threshold.
            return self._vad.speech_segments(pcm, min_speech_ms=0)
