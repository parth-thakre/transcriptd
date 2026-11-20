"""ggml engine: transcribe.cpp bindings (Parakeet, Whisper, Nemotron, Canary, ...).

Runs on CPU/Vulkan from the PyPI wheel, or on HIP/CUDA from a locally built
libtranscribe (set TRANSCRIBE_LIBRARY; see scripts/build-transcribe-cpp.sh).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path

import numpy as np

from ..audio import pad_to_min, trim_trailing_silence
from .base import Transcript, TranscribeRequest, Word

log = logging.getLogger(__name__)

ERR_OUTPUT_REPETITION = 19  # transcribe_status the 0.2.x binding has no exception class for


def _word_conf(token_ps: list[float]) -> float:
    """Geometric mean of token probabilities; NaN tokens are ignored."""
    ps = [p for p in token_ps if p == p and p > 0]
    if not ps:
        return 1.0
    return float(math.exp(sum(math.log(p) for p in ps) / len(ps)))


_log_bridge_installed = False


def _install_log_bridge(tc) -> None:
    """Route ggml/transcribe.cpp native logs into Python logging at DEBUG."""
    global _log_bridge_installed
    if _log_bridge_installed:
        return
    native = logging.getLogger("transcriptd.asr.ggml.native")

    def handler(level, msg):
        native.debug("%s", msg.rstrip() if isinstance(msg, str) else msg)

    try:
        tc.set_log_callback(handler)
        _log_bridge_installed = True
    except Exception as exc:  # pragma: no cover - binding without log support
        log.debug("no native log callback: %s", exc)


class GgmlEngine:
    name = "ggml"

    def __init__(
        self,
        model_path: str | Path,
        *,
        backend: str = "auto",
        n_threads: int = 0,
        whisper_prompt_bias: bool = False,
        pnc: str = "default",
        itn: str = "default",
    ):
        import transcribe_cpp

        _install_log_bridge(transcribe_cpp)
        self._tc = transcribe_cpp
        self.model_path = Path(model_path)
        self.model_name = self.model_path.stem
        self._model = transcribe_cpp.Model(str(self.model_path), backend=backend)
        self._session = self._model.session(n_threads=n_threads)
        self._lock = threading.Lock()  # one run in flight per Model (0.x limitation)
        self.caps = self._model.capabilities
        self.arch = self._model.arch
        self.device = self._model.backend
        self.whisper_prompt_bias = whisper_prompt_bias
        # Whisper decodes 30 s windows (and stitches them itself); the other families here take a
        # whole take in one pass. See asr/chunking.py.
        self.window_ms = 30_000 if self.arch == "whisper" else None
        self.long_form = True
        self.pnc, self.itn = pnc, itn
        log.info("ggml engine loaded %s (%s/%s) on %s, max_ts=%s streaming=%s",
                 self.model_name, self.arch, self._model.variant, self.device,
                 self.caps.max_timestamp_kind, self.caps.supports_streaming)

    # -- Engine protocol -----------------------------------------------------------

    def supports_streaming(self) -> bool:
        return bool(self.caps.supports_streaming)

    def close(self) -> None:
        with self._lock:
            self._session.close()
            self._model.close()

    def transcribe(self, req: TranscribeRequest) -> Transcript:
        pcm = np.ascontiguousarray(req.pcm, dtype=np.float32)
        pcm = trim_trailing_silence(pcm)
        if pcm.size == 0:
            return Transcript(words=[], language=req.language, engine=self.name, model=self.model_name)
        pcm = pad_to_min(pcm)

        family = None
        if self.arch == "whisper" and self.whisper_prompt_bias and req.prompt_terms:
            # Documented to seed hallucinations; off by default, opt-in per profile.
            family = self._tc.WhisperRunOptions(initial_prompt=", ".join(req.prompt_terms[:60]))

        ts = "token" if self.caps.max_timestamp_kind == "token" else self.caps.max_timestamp_kind
        t0 = time.perf_counter()
        try:
            with self._lock:
                res = self._session.run(
                    pcm,
                    language=req.language,
                    timestamps=ts,
                    pnc=self.pnc,
                    itn=self.itn,
                    diarize="on" if req.diarize else "default",
                    family=family,
                )
        except self._tc.errors.OutputTruncated as exc:
            # The decode budget ran out: keep what was decoded rather than failing the dictation.
            if exc.partial_result is None:
                raise
            log.warning("%s: output truncated, using the partial transcript", self.model_name)
            res = exc.partial_result
        except self._tc.errors.TranscribeError as exc:
            # The decoder looped ("Oh, oh, oh ..." on noise) and stopped: nothing in it is speech.
            if exc.status != ERR_OUTPUT_REPETITION:
                raise
            log.warning("%s: repetition stop, no transcript", self.model_name)
            return Transcript(words=[], language=req.language, engine=self.name, model=self.model_name)
        wall = (time.perf_counter() - t0) * 1000
        words, scored = self._words_from(res)
        return Transcript(
            words=words,
            language=res.language or req.language,
            scored=scored,
            engine=self.name,
            model=self.model_name,
            timings_ms={
                "wall": round(wall, 1),
                "encode": round(res.timings.encode_ms, 1),
                "decode": round(res.timings.decode_ms, 1),
            },
        )

    # -- helpers -------------------------------------------------------------------

    def _words_from(self, res) -> tuple[list[Word], bool]:
        if res.words:
            out = []
            scored = bool(res.tokens)
            for w in res.words:
                text = w.text.strip()
                if not text:
                    continue
                toks = res.tokens[w.first_token : w.first_token + w.n_tokens] if res.tokens else []
                out.append(Word(text=text, start_ms=w.t0_ms, end_ms=w.t1_ms, conf=_word_conf([t.p for t in toks])))
            return out, scored
        # Segment-only models (Whisper): split segment text on whitespace and spread the
        # segment span evenly. Confidence comes from tokens when present.
        out = []
        scored = False
        for seg in res.segments:
            parts = seg.text.split()
            if not parts:
                continue
            span = max(seg.t1_ms - seg.t0_ms, 1)
            step = span / len(parts)
            for i, p in enumerate(parts):
                out.append(Word(text=p, start_ms=int(seg.t0_ms + i * step), end_ms=int(seg.t0_ms + (i + 1) * step)))
        if not out and res.text.strip():
            out = [Word(text=p, start_ms=0, end_ms=0) for p in res.text.split()]
        return out, scored


class GgmlPartialStream:
    """Live partials from a streaming-capable ggml model (Nemotron, Parakeet stream)."""

    def __init__(self, engine: GgmlEngine, language: str | None):
        self._engine = engine
        engine._lock.acquire()
        try:
            try:
                self._stream = engine._session.stream(language=language, timestamps="none")
            except engine._tc.errors.UnsupportedRequest:
                if language is None:
                    raise
                # Some streaming families (Nemotron) take no language hint at all.
                self._stream = engine._session.stream(language=None, timestamps="none")
        except Exception:
            engine._lock.release()
            raise

    def feed(self, pcm: np.ndarray) -> str:
        self._stream.feed(np.ascontiguousarray(pcm, dtype=np.float32))
        return self._stream.text().display

    def finish(self) -> Transcript:
        self._stream.finalize()
        res = self._stream.snapshot()
        words, scored = self._engine._words_from(res)
        return Transcript(words=words, language=res.language, scored=scored,
                          engine=self._engine.name, model=self._engine.model_name)

    def close(self) -> None:
        try:
            self._stream.reset()
        finally:
            self._engine._lock.release()
