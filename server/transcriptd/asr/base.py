"""Engine-neutral transcript types and the engine interface.

Every engine returns a Transcript made of Words with timestamps and a confidence in
[0, 1] (NaN-free; engines that cannot score a word return 1.0 and set `scored=False`
on the transcript so downstream code knows not to trust the numbers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(slots=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    conf: float = 1.0

    def to_dict(self) -> dict:
        return {"text": self.text, "start_ms": self.start_ms, "end_ms": self.end_ms, "conf": round(self.conf, 3)}


@dataclass(slots=True)
class Transcript:
    words: list[Word]
    language: str | None = None
    scored: bool = True  # False when conf values are placeholders
    engine: str = ""
    model: str = ""
    timings_ms: dict[str, float] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)  # ensemble: each member profile's raw text

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()

    def to_dict(self) -> dict:
        d = {
            "text": self.text,
            "language": self.language,
            "scored": self.scored,
            "engine": self.engine,
            "model": self.model,
            "words": [w.to_dict() for w in self.words],
            "timings_ms": self.timings_ms,
        }
        if self.members:
            d["members"] = self.members
        return d


@dataclass(slots=True)
class TranscribeRequest:
    pcm: np.ndarray  # float32 mono 16 kHz
    language: str | None = None  # None = auto/detect where the engine supports it
    prompt_terms: list[str] = field(default_factory=list)  # vocab terms for biasing
    diarize: bool = False


@runtime_checkable
class Engine(Protocol):
    name: str
    model_name: str

    def transcribe(self, req: TranscribeRequest) -> Transcript: ...

    def supports_streaming(self) -> bool: ...

    def close(self) -> None: ...


class StreamingEngine(Protocol):
    """Optional: live partials for the overlay. Partials are never injected."""

    def open_stream(self, language: str | None) -> "PartialStream": ...


class PartialStream(Protocol):
    def feed(self, pcm: np.ndarray) -> str: ...  # returns current display text
    def finish(self) -> Transcript: ...
    def close(self) -> None: ...
