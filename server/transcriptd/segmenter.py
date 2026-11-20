"""Streaming speech segmenter on top of Silero VAD.

Feed 16 kHz float32 audio in any chunk size; get back (pcm, start_ms, end_ms) speech
segments once they end (enough trailing silence) or hit the maximum length.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import SAMPLE_RATE
from .vad import WINDOW, SileroVAD


@dataclass(slots=True)
class Segment:
    pcm: np.ndarray
    start_ms: int
    end_ms: int


class StreamSegmenter:
    def __init__(
        self,
        vad: SileroVAD,
        *,
        threshold: float = 0.5,
        min_silence_ms: int = 600,
        min_speech_ms: int = 300,
        max_segment_ms: int = 25_000,
        pad_ms: int = 150,
    ):
        self.vad = vad
        self.threshold = threshold
        self.min_silence = min_silence_ms
        self.min_speech = min_speech_ms
        self.max_segment = max_segment_ms
        self.pad = pad_ms
        self._pending = np.zeros(0, dtype=np.float32)  # not yet VAD-scored
        self._audio: list[np.ndarray] = []  # windows since segment start (incl. pre-pad)
        self._prepad: list[np.ndarray] = []  # last few windows before speech
        self._in_speech = False
        self._silence_ms = 0.0
        self._seg_start_ms = 0.0
        self._last_voiced_ms = 0.0
        self._pos_ms = 0.0  # absolute time at end of last scored window
        self.vad.reset()

    @property
    def window_ms(self) -> float:
        return WINDOW * 1000 / SAMPLE_RATE

    def feed(self, pcm: np.ndarray) -> list[Segment]:
        out: list[Segment] = []
        self._pending = np.concatenate([self._pending, pcm.astype(np.float32, copy=False)])
        while len(self._pending) >= WINDOW:
            win = self._pending[:WINDOW]
            self._pending = self._pending[WINDOW:]
            out.extend(self._score(win))
        return out

    def flush(self) -> list[Segment]:
        """End of stream: emit whatever is in progress."""
        out: list[Segment] = []
        if len(self._pending):
            out.extend(self._score(np.pad(self._pending, (0, WINDOW - len(self._pending)))))
            self._pending = np.zeros(0, dtype=np.float32)
        if self._in_speech:
            out.extend(self._close(self._pos_ms))
        return out

    # -- internals -------------------------------------------------------------------

    def _score(self, win: np.ndarray) -> list[Segment]:
        p = self.vad.prob(win)
        t0 = self._pos_ms
        self._pos_ms += self.window_ms
        out: list[Segment] = []
        if self._in_speech:
            self._audio.append(win)
            if p >= self.threshold:
                self._silence_ms = 0.0
                self._last_voiced_ms = self._pos_ms
            else:
                self._silence_ms += self.window_ms
            if self._silence_ms >= self.min_silence:
                out.extend(self._close(self._last_voiced_ms))
            elif self._pos_ms - self._seg_start_ms >= self.max_segment:
                out.extend(self._close(self._pos_ms))
        else:
            self._prepad.append(win)
            keep = max(1, int(self.pad / self.window_ms))
            self._prepad = self._prepad[-keep:]
            if p >= self.threshold:
                self._in_speech = True
                self._silence_ms = 0.0
                self._audio = list(self._prepad)
                self._seg_start_ms = t0 - (len(self._prepad) - 1) * self.window_ms
                self._last_voiced_ms = self._pos_ms
                self._prepad = []
        return out

    def _close(self, end_ms: float) -> list[Segment]:
        self._in_speech = False
        audio = np.concatenate(self._audio) if self._audio else np.zeros(0, dtype=np.float32)
        self._audio = []
        start = max(0.0, self._seg_start_ms)
        end = min(end_ms + self.pad, self._pos_ms)
        n = int((end - start) * SAMPLE_RATE / 1000)
        pcm = audio[:n]
        if end - start < self.min_speech or pcm.size == 0:
            return []
        return [Segment(pcm=pcm, start_ms=int(start), end_ms=int(end))]
