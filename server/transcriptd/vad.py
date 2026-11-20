"""Silero VAD v5/v6 via onnxruntime (CPU). Used for meeting segmentation and end-of-speech."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)

# A tagged release, pinned by hash: the master branch moves, and an error page must never be
# cached as the model.
SILERO_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/src/silero_vad/data/silero_vad.onnx"
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
WINDOW = 512  # samples at 16 kHz (32 ms), fixed by the model


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str) -> bytes:
    """The file at `url`; raises on an HTTP error status. Swapped out in tests."""
    import httpx

    r = httpx.get(url, follow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.content


def ensure_model(models_dir: Path) -> Path:
    """models/silero_vad.onnx, downloaded (again) unless the file there has the pinned hash."""
    p = models_dir / "silero_vad.onnx"
    if p.exists() and _sha256(p) == SILERO_SHA256:
        return p
    log.info("downloading silero VAD to %s", p)
    data = _download(SILERO_URL)
    got = hashlib.sha256(data).hexdigest()
    if got != SILERO_SHA256:
        raise RuntimeError(f"silero VAD download from {SILERO_URL} has sha256 {got}, expected {SILERO_SHA256}")
    # A temp file of its own (two first downloads at once must not share one), renamed into place:
    # the last rename wins, and both are the same verified bytes.
    fd, tmp = tempfile.mkstemp(dir=models_dir, prefix=p.name + ".", suffix=".part")
    part = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(part, p)
    finally:
        part.unlink(missing_ok=True)
    return p


class SileroVAD:
    def __init__(self, model_path: Path, threshold: float = 0.5):
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1
        self._sess = ort.InferenceSession(str(model_path), so, providers=["CPUExecutionProvider"])
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        """Speech probability for one 512-sample frame. Keeps streaming state."""
        if frame.shape[0] != WINDOW:
            frame = np.pad(frame, (0, WINDOW - frame.shape[0]))
        x = np.concatenate([self._context, frame[None, :].astype(np.float32)], axis=1)
        out, self._state = self._sess.run(
            None, {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
        )
        self._context = x[:, -64:]
        return float(out[0][0])

    def speech_segments(self, pcm: np.ndarray, min_speech_ms: int = 250, min_silence_ms: int = 400, pad_ms: int = 120) -> list[tuple[int, int]]:
        """Offline: (start_ms, end_ms) spans of speech in a float32 16 kHz buffer."""
        self.reset()
        probs = [self.prob(pcm[i : i + WINDOW]) for i in range(0, len(pcm), WINDOW)]
        frame_ms = WINDOW * 1000 / SAMPLE_RATE
        segs: list[tuple[int, int]] = []
        start = None
        silence = 0.0
        for i, p in enumerate(probs):
            t = i * frame_ms
            if p >= self.threshold:
                if start is None:
                    start = t
                silence = 0.0
            elif start is not None:
                silence += frame_ms
                if silence >= min_silence_ms:
                    end = t - silence + frame_ms
                    if end - start >= min_speech_ms:
                        segs.append((max(0, int(start - pad_ms)), int(end + pad_ms)))
                    start, silence = None, 0.0
        if start is not None:
            end = len(probs) * frame_ms
            if end - start >= min_speech_ms:
                segs.append((max(0, int(start - pad_ms)), int(min(end + pad_ms, len(pcm) * 1000 / SAMPLE_RATE))))
        return segs
