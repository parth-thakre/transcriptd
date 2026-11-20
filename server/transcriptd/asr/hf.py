"""HF transformers engine for models without a ggml port (Indic-Transcribe-Flex, Whisper
fine-tunes such as Trelis/whisper-hinglish-preview).

Requires the `hf` extra and a torch build for your GPU (ROCm/CUDA) or CPU. Word
timestamps come from the pipeline's `return_timestamps="word"` where the model supports
it; otherwise words are spread across the chunk span and the transcript is unscored.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from ..audio import SAMPLE_RATE
from .base import Transcript, TranscribeRequest, Word

log = logging.getLogger(__name__)


class HfEngine:
    name = "hf"

    def __init__(self, model_id: str, *, language: str | None = None, device: str | None = None, dtype: str = "auto",
                 generate_kwargs: dict | None = None, mixedcode: bool = False):
        import torch

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"  # ROCm torch also reports cuda
        torch_dtype = {"auto": torch.float16 if device != "cpu" else torch.float32, "float32": torch.float32,
                       "float16": torch.float16, "bfloat16": torch.bfloat16}[dtype]
        self.model_name = model_id
        self.device = device
        self.language = language
        self.mixedcode = mixedcode
        self.window_ms = 30_000  # Whisper-family checkpoints
        self.long_form = not mixedcode  # the HF pipeline strides windows; the mixed-code path cuts blind
        self._gen = generate_kwargs or {}
        self._dtype = torch_dtype
        self._pipe = None
        self._mc = None
        if mixedcode:
            self._mc = _MixedCodeWhisper(model_id, device, torch_dtype)
        else:
            from transformers import pipeline

            self._pipe = pipeline("automatic-speech-recognition", model=model_id, device=device, torch_dtype=torch_dtype,
                                  trust_remote_code=True)
        log.info("hf engine loaded %s on %s (%s)%s", model_id, device, torch_dtype, " mixedcode" if mixedcode else "")

    def supports_streaming(self) -> bool:
        return False

    def close(self) -> None:
        self._pipe = None
        self._mc = None

    def transcribe(self, req: TranscribeRequest) -> Transcript:
        lang = req.language or self.language
        if self._mc is not None:
            t0 = time.perf_counter()
            words = self._mc.transcribe(np.asarray(req.pcm, dtype=np.float32), lang or "hi")
            return Transcript(words=words, language=lang, scored=False, engine=self.name, model=self.model_name,
                              timings_ms={"wall": round((time.perf_counter() - t0) * 1000, 1)})
        gen = dict(self._gen)
        if lang:
            gen["language"] = lang
        t0 = time.perf_counter()
        try:
            out = self._pipe({"raw": np.asarray(req.pcm, dtype=np.float32), "sampling_rate": SAMPLE_RATE},
                             return_timestamps="word", chunk_length_s=30, generate_kwargs=gen)
            chunks = out.get("chunks") or []
        except Exception as exc:  # model lacks word timestamps
            log.debug("word timestamps unavailable for %s: %s", self.model_name, exc)
            out = self._pipe({"raw": np.asarray(req.pcm, dtype=np.float32), "sampling_rate": SAMPLE_RATE},
                             chunk_length_s=30, generate_kwargs=gen)
            chunks = []
        wall = (time.perf_counter() - t0) * 1000
        words: list[Word] = []
        for c in chunks:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            s, e = c.get("timestamp") or (0, 0)
            s_ms, e_ms = int((s or 0) * 1000), int((e or s or 0) * 1000)
            parts = text.split()
            if len(parts) == 1:
                words.append(Word(text=text, start_ms=s_ms, end_ms=e_ms))
            else:
                step = max(e_ms - s_ms, 1) / len(parts)
                words.extend(Word(text=p, start_ms=int(s_ms + i * step), end_ms=int(s_ms + (i + 1) * step)) for i, p in enumerate(parts))
        if not words:
            words = [Word(text=t, start_ms=0, end_ms=0) for t in (out.get("text") or "").split()]
        return Transcript(words=words, language=lang, scored=False, engine=self.name, model=self.model_name,
                          timings_ms={"wall": round(wall, 1)})


class _MixedCodeWhisper:
    """Whisper fine-tunes with a <|mixedcode|> token (Trelis/whisper-hinglish-preview):
    the prompt is [sot, <|lang|>, <|mixedcode|>, transcribe, notimestamps]. No word
    timestamps in this mode; words are spread evenly over each 30 s window."""

    WINDOW_S = 30

    def __init__(self, model_id: str, device: str, dtype):
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        self.torch = torch
        self.proc = WhisperProcessor.from_pretrained(model_id)
        self.model = WhisperForConditionalGeneration.from_pretrained(model_id, torch_dtype=dtype).to(device).eval()
        self.device, self.dtype = device, dtype
        tok = self.proc.tokenizer
        self._mc_ids = tok("<|mixedcode|>", add_special_tokens=False).input_ids
        self._sot = tok.convert_tokens_to_ids("<|startoftranscript|>")
        self._transcribe = tok.convert_tokens_to_ids("<|transcribe|>")
        self._nots = tok.convert_tokens_to_ids("<|notimestamps|>")

    def _prompt(self, lang: str) -> list[int]:
        lang_id = self.proc.tokenizer.convert_tokens_to_ids(f"<|{lang}|>")
        return [self._sot, lang_id, *self._mc_ids, self._transcribe, self._nots]

    def transcribe(self, pcm: np.ndarray, lang: str) -> list[Word]:
        torch = self.torch
        words: list[Word] = []
        win = self.WINDOW_S * SAMPLE_RATE
        prompt = torch.tensor([self._prompt(lang)], device=self.device)
        for off in range(0, max(len(pcm), 1), win):
            chunk = pcm[off : off + win]
            if chunk.size < SAMPLE_RATE // 10:
                break
            feat = self.proc.feature_extractor(chunk, sampling_rate=SAMPLE_RATE, return_tensors="pt").input_features
            feat = feat.to(self.device, self.dtype)
            with torch.inference_mode():
                out = self.model.generate(input_features=feat, decoder_input_ids=prompt, max_new_tokens=440,
                                          num_beams=1, do_sample=False)
            text = self.proc.tokenizer.decode(out[0], skip_special_tokens=True).strip()
            parts = text.split()
            if not parts:
                continue
            start_ms = off * 1000 // SAMPLE_RATE
            span = min(len(chunk), win) * 1000 / SAMPLE_RATE
            step = span / len(parts)
            words.extend(Word(text=p, start_ms=int(start_ms + i * step), end_ms=int(start_ms + (i + 1) * step)) for i, p in enumerate(parts))
        return words
