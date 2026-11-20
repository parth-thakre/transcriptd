"""GgmlEngine error handling and model-file resolution, without loading a model."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from transcriptd.asr import ggml
from transcriptd.asr.base import TranscribeRequest
from transcriptd.asr.registry import resolve_ggml_model


class _Errors:
    class TranscribeError(RuntimeError):
        def __init__(self, msg, status=0):
            super().__init__(msg)
            self.status = status

    class OutputTruncated(TranscribeError):
        partial_result = None


def _result(text):
    seg = SimpleNamespace(text=text, t0_ms=0, t1_ms=1000)
    return SimpleNamespace(words=[], tokens=[], segments=[seg], text=text, language="en",
                           timings=SimpleNamespace(encode_ms=1.0, decode_ms=1.0))


def _engine(run):
    e = object.__new__(ggml.GgmlEngine)
    e._tc = SimpleNamespace(errors=_Errors)
    e._session = SimpleNamespace(run=run)
    e._lock = threading.Lock()
    e.caps = SimpleNamespace(max_timestamp_kind="segment")
    e.arch, e.whisper_prompt_bias, e.pnc, e.itn = "qwen3_asr", False, "default", "default"
    e.model_name = "fake"
    return e


def _req():
    t = np.arange(16000 * 2) / 16000
    return TranscribeRequest(pcm=(0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), language="en")


def test_a_truncated_decode_keeps_the_partial_transcript():
    def run(*a, **k):
        exc = _Errors.OutputTruncated("budget")
        exc.partial_result = _result("so the first part was")
        raise exc

    assert _engine(run).transcribe(_req()).text == "so the first part was"


def test_a_repetition_stop_is_no_transcript_not_a_failed_dictation():
    def run(*a, **k):
        raise _Errors.TranscribeError("repetition", status=ggml.ERR_OUTPUT_REPETITION)

    assert _engine(run).transcribe(_req()).words == []


def test_other_decode_errors_still_raise():
    def run(*a, **k):
        raise _Errors.TranscribeError("backend", status=7)

    with pytest.raises(_Errors.TranscribeError):
        _engine(run).transcribe(_req())


def test_quant_f16_never_picks_bf16(tmp_path):
    d = tmp_path / "Qwen3-ASR-1.7B"
    d.mkdir()
    for n in ("Qwen3-ASR-1.7B-BF16.gguf", "Qwen3-ASR-1.7B-F16.gguf", "Qwen3-ASR-1.7B-Q8_0.gguf"):
        (d / n).write_bytes(b"")
    pick = lambda q: resolve_ggml_model("handy-computer/Qwen3-ASR-1.7B-gguf", q, tmp_path).name  # noqa: E731
    assert pick("F16") == "Qwen3-ASR-1.7B-F16.gguf"
    assert pick("BF16") == "Qwen3-ASR-1.7B-BF16.gguf"
    assert pick("Q8_0") == "Qwen3-ASR-1.7B-Q8_0.gguf"
