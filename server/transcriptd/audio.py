"""PCM helpers. The whole server speaks float32 mono 16 kHz internally."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000


def int16_bytes_to_float32(buf: bytes) -> np.ndarray:
    return np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_int16_bytes(pcm: np.ndarray) -> bytes:
    return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    if src_rate == dst_rate:
        return pcm.astype(np.float32, copy=False)
    import soxr

    return soxr.resample(pcm.astype(np.float32, copy=False), src_rate, dst_rate).astype(np.float32)


def load_audio(path_or_bytes: str | Path | bytes) -> tuple[np.ndarray, int]:
    """Decode WAV/FLAC/OGG via libsndfile to float32 mono at 16 kHz. Returns (pcm, orig_rate)."""
    import soundfile as sf

    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else str(path_or_bytes)
    data, rate = sf.read(src, dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    return resample(mono, rate), rate


def rms_db(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
    return 20.0 * np.log10(max(r, 1e-9))


def audio_stats(pcm: np.ndarray) -> dict:
    """How a take sounded, from 20 ms frames, in dBFS: speech level (loudest tenth of frames),
    noise floor (quietest tenth), their gap, silence before and after the speech, clipping, and
    the share of energy under 120 Hz (handling and wind rumble; speech has little there).
    Kept with every dictation so a misheard one can be told apart from a bad recording."""
    frame = SAMPLE_RATE // 50
    n = len(pcm) // frame
    if n == 0:
        return {}
    x = pcm[: n * frame].astype(np.float64)
    db = 20.0 * np.log10(np.maximum(np.sqrt((x.reshape(n, frame) ** 2).mean(axis=1)), 1e-5))  # floor: -100 dB
    speech, noise = float(np.percentile(db, 90)), float(np.percentile(db, 10))
    voiced = np.flatnonzero(db > noise + 10.0)
    head = x[: 60 * SAMPLE_RATE]
    power = np.abs(np.fft.rfft(head)) ** 2
    freqs = np.fft.rfftfreq(len(head), 1.0 / SAMPLE_RATE)
    return {
        "peak_db": round(20.0 * np.log10(max(float(np.abs(x).max()), 1e-9)), 1),
        "rms_db": round(rms_db(x), 1),
        "speech_db": round(speech, 1),
        "noise_db": round(noise, 1),
        "snr_db": round(speech - noise, 1),
        "clipped_pct": round(float((np.abs(x) >= 0.999).mean()) * 100.0, 3),
        "lead_ms": int(voiced[0] * 20) if voiced.size else None,
        "tail_ms": int((n - 1 - voiced[-1]) * 20) if voiced.size else None,
        "rumble_pct": round(float(power[freqs < 120].sum() / max(power.sum(), 1e-18)) * 100.0, 1),
    }


def normalize_gain(pcm: np.ndarray, target_rms_db: float = -22.0, only_below_db: float = -30.0, max_gain_db: float = 30.0) -> tuple[np.ndarray, float]:
    """Bring quiet recordings (whispering, distant mic) up to a level the models expect.

    Only boosts; never attenuates. Gain is capped and the result is soft-clipped so a
    breath or click cannot blow up. Returns (pcm, gain_db_applied).
    """
    level = rms_db(pcm)
    if pcm.size == 0 or level >= only_below_db:
        return pcm, 0.0
    gain_db = min(target_rms_db - level, max_gain_db)
    out = pcm.astype(np.float32) * (10.0 ** (gain_db / 20.0))
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.98:
        out = np.tanh(out / peak * 1.5) / np.tanh(1.5) * 0.98
    return out.astype(np.float32), float(gain_db)


def trim_trailing_silence(pcm: np.ndarray, threshold_db: float = -58.0, frame_ms: int = 20, keep_ms: int = 200) -> np.ndarray:
    """Cut trailing near-silence but keep a short tail so the last word is not clipped."""
    frame = SAMPLE_RATE * frame_ms // 1000
    n_frames = len(pcm) // frame
    last_voiced = -1
    for i in range(n_frames):
        if rms_db(pcm[i * frame : (i + 1) * frame]) > threshold_db:
            last_voiced = i
    if last_voiced < 0:
        return pcm[:0]
    end = min(len(pcm), (last_voiced + 1) * frame + SAMPLE_RATE * keep_ms // 1000)
    return pcm[:end]


def pad_to_min(pcm: np.ndarray, min_ms: int = 1000, lead_ms: int = 100) -> np.ndarray:
    """Prepend a little silence and make sure the clip is at least `min_ms` long.

    Parakeet returns nothing for very short clips that end in silence, so we pad at
    the front rather than the back.
    """
    lead = np.zeros(SAMPLE_RATE * lead_ms // 1000, dtype=np.float32)
    out = np.concatenate([lead, pcm])
    need = SAMPLE_RATE * min_ms // 1000 - len(out)
    if need > 0:
        out = np.concatenate([np.zeros(need, dtype=np.float32), out])
    return out
