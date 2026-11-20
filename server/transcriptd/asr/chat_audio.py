"""Audio-capable chat models as an ASR engine (Gemma 4 E4B via llama-server, or any
OpenAI-compatible /v1/chat/completions that accepts `input_audio` content parts).

No word timestamps or confidences: words are spread evenly over the clip and the
transcript is marked unscored. Clips longer than `window_s` are split.
"""

from __future__ import annotations

import base64
import io
import time

import httpx
import numpy as np
import soundfile as sf

from ..audio import SAMPLE_RATE
from .base import Transcript, TranscribeRequest, Word

DEFAULT_PROMPT = (
    "Transcribe this audio exactly, word for word, in the language(s) spoken and their native script. "
    "Do not translate, summarize, correct grammar, or omit anything. Output only the transcript."
)


class ChatAudioEngine:
    name = "chat_audio"

    def __init__(self, base_url: str, api_key: str, model: str, *, prompt: str | None = None, window_s: int = 30,
                 timeout: float = 120.0, extra: dict | None = None):
        if not base_url:
            raise ValueError("chat_audio profile needs base_url")
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.device = "remote"
        self.prompt = prompt or DEFAULT_PROMPT
        self.window_s = window_s
        self.window_ms = window_s * 1000
        self.extra = extra or {}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(timeout=timeout, headers=headers)

    def supports_streaming(self) -> bool:
        return False

    def close(self) -> None:
        self._client.close()

    def _ask(self, pcm: np.ndarray, language: str | None) -> str:
        buf = io.BytesIO()
        sf.write(buf, pcm, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        prompt = self.prompt
        if language:
            prompt += f" The speech is mainly in language code '{language}'."
        body = {
            "model": self.model_name,
            "temperature": 0.0,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(buf.getvalue()).decode(), "format": "wav"}},
            ]}],
            **self.extra,
        }
        r = self._client.post(f"{self.base_url}/chat/completions", json=body)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1]
        return text

    def transcribe(self, req: TranscribeRequest) -> Transcript:
        pcm = np.asarray(req.pcm, dtype=np.float32)
        win = self.window_s * SAMPLE_RATE
        words: list[Word] = []
        t0 = time.perf_counter()
        for off in range(0, max(len(pcm), 1), win):
            chunk = pcm[off : off + win]
            if chunk.size < SAMPLE_RATE // 10:
                break
            parts = self._ask(chunk, req.language).split()
            if not parts:
                continue
            start_ms = off * 1000 // SAMPLE_RATE
            step = (len(chunk) * 1000 / SAMPLE_RATE) / len(parts)
            words.extend(Word(text=p, start_ms=int(start_ms + i * step), end_ms=int(start_ms + (i + 1) * step)) for i, p in enumerate(parts))
        return Transcript(words=words, language=req.language, scored=False, engine=self.name, model=self.model_name,
                          timings_ms={"wall": round((time.perf_counter() - t0) * 1000, 1)})
