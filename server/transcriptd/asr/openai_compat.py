"""Escape-hatch engine: any /v1/audio/transcriptions endpoint (Speaches, vLLM, hosted)."""

from __future__ import annotations

import io
import time

import httpx
import soundfile as sf

from ..audio import SAMPLE_RATE
from .base import Transcript, TranscribeRequest, Word


class OpenAICompatEngine:
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        if not base_url:
            raise ValueError("openai_compat profile needs base_url")
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.device = "remote"
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})

    def supports_streaming(self) -> bool:
        return False

    def close(self) -> None:
        self._client.close()

    def transcribe(self, req: TranscribeRequest) -> Transcript:
        buf = io.BytesIO()
        sf.write(buf, req.pcm, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        buf.seek(0)
        data = {"model": self.model_name, "response_format": "verbose_json", "timestamp_granularities[]": "word"}
        if req.language:
            data["language"] = req.language
        if req.prompt_terms:
            data["prompt"] = ", ".join(req.prompt_terms)
        t0 = time.perf_counter()
        r = self._client.post(f"{self.base_url}/audio/transcriptions", data=data, files={"file": ("audio.wav", buf, "audio/wav")})
        r.raise_for_status()
        j = r.json()
        wall = (time.perf_counter() - t0) * 1000
        words = []
        for w in j.get("words") or []:
            words.append(Word(text=w["word"].strip(), start_ms=int(w.get("start", 0) * 1000), end_ms=int(w.get("end", 0) * 1000)))
        if not words:
            words = [Word(text=t, start_ms=0, end_ms=0) for t in (j.get("text") or "").split()]
        return Transcript(words=words, language=j.get("language") or req.language, scored=False,
                          engine=self.name, model=self.model_name, timings_ms={"wall": round(wall, 1)})
