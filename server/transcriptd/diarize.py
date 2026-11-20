"""Speaker diarization for the "them" channel. Optional: needs the `diarize` extra
(pyannote.audio) and an HF token that has accepted the pyannote model terms.

Returns (start_ms, end_ms, speaker_label) turns. When unavailable, callers keep a
single "them" speaker.
"""

from __future__ import annotations

import logging
import os

import numpy as np

log = logging.getLogger(__name__)

_pipeline = None
_tried = False

MODEL = "pyannote/speaker-diarization-community-1"


def available() -> bool:
    global _pipeline, _tried
    if _pipeline is not None:
        return True
    if _tried:
        return False
    _tried = True
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError:
        log.info("diarization off: install the 'diarize' extra (pyannote.audio + torch)")
        return False
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    try:
        p = Pipeline.from_pretrained(MODEL, token=token)
    except Exception as exc:
        log.warning("diarization off: could not load %s (%s)", MODEL, exc)
        return False
    if torch.cuda.is_available():
        p.to(torch.device("cuda"))
    _pipeline = p
    log.info("diarization ready (%s)", MODEL)
    return True


def diarize(pcm: np.ndarray, sample_rate: int = 16000) -> list[tuple[int, int, str]]:
    if not available():
        return []
    import torch

    waveform = torch.from_numpy(np.asarray(pcm, dtype=np.float32))[None, :]
    out = _pipeline({"waveform": waveform, "sample_rate": sample_rate})
    ann = getattr(out, "speaker_diarization", out)
    turns = []
    for turn, _, speaker in ann.itertracks(yield_label=True):
        turns.append((int(turn.start * 1000), int(turn.end * 1000), str(speaker)))
    return turns


def speaker_at(turns: list[tuple[int, int, str]], start_ms: int, end_ms: int) -> str | None:
    """Speaker with the most overlap with [start, end), or None."""
    best, best_ov = None, 0
    for s, e, spk in turns:
        ov = min(e, end_ms) - max(s, start_ms)
        if ov > best_ov:
            best, best_ov = spk, ov
    return best
