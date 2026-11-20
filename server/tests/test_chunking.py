"""Long takes (asr/chunking.py): synthetic audio, fake engines, fake or real Silero VAD."""

from itertools import pairwise

import numpy as np
import pytest
import soundfile as sf

from tests.test_pipeline import pipeline  # noqa: F401  (fixture)
from transcriptd.asr.base import Transcript, Word
from transcriptd.asr.chunking import plan_chunks

SR = 16000
FIXTURES = __import__("pathlib").Path(__file__).resolve().parent / "fixtures"


def _take(seconds: float) -> np.ndarray:
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(seconds * SR)) * 0.01).astype(np.float32)


class WindowedEngine:
    """Whisper-shaped fake: decodes 30 s windows and, like Whisper on a silent last window,
    invents "Thanks for watching!" at 30 s when the audio runs past the window. Records what
    it was given."""

    name = "fake"
    model_name = "fake"

    def __init__(self, window_ms=30_000, long_form=True):
        self.window_ms = window_ms
        self.long_form = long_form
        self.calls: list[int] = []  # ms of audio per call

    def transcribe(self, req):
        ms = int(len(req.pcm) * 1000 / SR)
        self.calls.append(ms)
        words = [Word("speech", 0, 400), Word("ends", max(0, ms - 600), max(0, ms - 200))]
        if self.window_ms and ms > self.window_ms:
            words += [Word("Thanks", 30_000, 40_000), Word("for", 40_000, 50_000), Word("watching!", 50_000, 60_000)]
        return Transcript(words=words, language="en", scored=False, engine="fake", model="fake")

    def supports_streaming(self):
        return False

    def close(self):
        pass


def _setup(pipeline, engine, spans):  # noqa: F811
    p, reg, _ = pipeline
    reg._engines[reg.settings.default_profile] = engine
    seen = []

    def fake_vad(pcm):
        seen.append(len(pcm))
        return spans

    p.speech_spans = fake_vad
    return p, seen


def test_silent_tail_past_the_window_is_dropped_so_nothing_is_invented(pipeline):  # noqa: F811
    # 30.26 s take, speech ends at 26.6 s, a click at the very end the VAD does not call speech.
    eng = WindowedEngine()
    p, seen = _setup(pipeline, eng, [(300, 13_500), (14_200, 26_600)])
    r = p.process(_take(30.26))
    assert seen, "a take past the window goes through the VAD"
    assert eng.calls == [27_100]  # head kept, tail cut 500 ms after the last speech
    assert "Thanks" not in r.raw_text and r.raw_text == "speech ends"


def test_long_form_engine_gets_the_take_whole_minus_the_tail(pipeline):  # noqa: F811
    eng = WindowedEngine()
    p, _ = _setup(pipeline, eng, [(800, 20_000), (21_000, 54_900)])
    p.process(_take(58.0))
    assert eng.calls == [55_400]  # one call: Whisper stitches its own windows


def test_engine_without_long_form_gets_pieces_cut_at_pauses(pipeline):  # noqa: F811
    eng = WindowedEngine(long_form=False)
    spans = [(0, 9_000), (9_600, 18_300), (19_100, 27_400), (27_700, 36_600), (37_000, 46_000), (47_000, 55_000)]
    p, _ = _setup(pipeline, eng, spans)
    r = p.process(_take(55.2))
    assert len(eng.calls) == 2 and all(ms <= 29_500 for ms in eng.calls)
    assert "Thanks" not in r.raw_text
    # Words come back on the take's clock, in order, and the cut is in the middle of a pause.
    starts = [w.start_ms for w in r.words]
    assert starts == sorted(starts)
    cut = eng.calls[0]
    assert any(a_end < cut < b_start for (_, a_end), (b_start, _) in pairwise(spans))
    assert r.words[2].start_ms == cut


def test_short_take_skips_the_vad(pipeline):  # noqa: F811
    eng = WindowedEngine()
    p, seen = _setup(pipeline, eng, [(0, 1000)])
    p.process(_take(29.9))
    assert seen == [] and eng.calls == [29_900]


def test_engine_without_a_window_skips_the_vad(pipeline):  # noqa: F811
    eng = WindowedEngine(window_ms=None)
    p, seen = _setup(pipeline, eng, [(0, 1000)])
    p.process(_take(60.0))
    assert seen == [] and eng.calls == [60_000]


@pytest.mark.parametrize("spans", [[], None])
def test_no_speech_found_or_no_vad_keeps_the_take_whole(pipeline, spans):  # noqa: F811
    eng = WindowedEngine(long_form=False)
    p, _ = _setup(pipeline, eng, spans)
    p.process(_take(31.0))
    assert eng.calls == [31_000]


def test_plan_chunks():
    # fewest pieces, each cut nearest the even split, never over the limit
    spans = [(0, 9_000), (9_600, 18_300), (19_100, 27_400), (27_700, 36_600), (37_000, 46_000), (47_000, 55_000)]
    assert plan_chunks(spans, 56_000, 29_500) == [(0, 27_550), (27_550, 55_500)]
    assert plan_chunks(spans, 56_000, None) == [(0, 55_500)]
    # three pieces when two cannot fit
    pieces = plan_chunks(spans, 56_000, 20_000)
    assert len(pieces) == 3 and all(b - a <= 20_000 for a, b in pieces)
    # speech with no pause is cut hard at the limit
    assert plan_chunks([(0, 50_000)], 50_000, 29_500, edge_pad_ms=0) == [(0, 25_000), (25_000, 50_000)]
    assert plan_chunks([], 31_000, 29_500) == [(0, 31_000)]


def test_real_vad_keeps_a_short_last_word_after_a_long_pause(pipeline):  # noqa: F811
    """A 200 ms word ("no") said after a long pause is shorter than the VAD's usual minimum
    speech span; the tail cut must still keep it."""
    from transcriptd.asr.chunking import SpeechSpans

    speech, _ = sf.read(FIXTURES / "jfk.wav", dtype="float32")
    rng = np.random.default_rng(0)
    noise = lambda s: (rng.standard_normal(int(s * SR)) * 0.003).astype(np.float32)  # noqa: E731
    word = speech[int(1.8 * SR): int(2.0 * SR)]
    take = np.concatenate([speech, noise(18), word, noise(3)])  # the word at ~29.0-29.2 s of 32.2 s
    eng = WindowedEngine()
    p, reg, _ = pipeline
    reg._engines[reg.settings.default_profile] = eng
    p.speech_spans = SpeechSpans(FIXTURES.parents[1] / "models")
    p.process(take)
    assert len(eng.calls) == 1 and eng.calls[0] > 29_200, eng.calls


def test_real_vad_drops_the_silent_tail_of_long_speech(pipeline):  # noqa: F811
    """Silero on public speech (JFK clip, 11 s) padded to 33 s with quiet noise: the engine gets
    the speech and not the 20+ s of noise after it."""
    from transcriptd.asr.chunking import SpeechSpans

    speech, rate = sf.read(FIXTURES / "jfk.wav", dtype="float32")
    assert rate == SR
    take = np.concatenate([speech, _take(33.0 - len(speech) / SR) * 0.1])
    eng = WindowedEngine()
    p, reg, _ = pipeline
    reg._engines[reg.settings.default_profile] = eng
    p.speech_spans = SpeechSpans(FIXTURES.parents[1] / "models")
    r = p.process(take)
    assert len(eng.calls) == 1 and 10_000 < eng.calls[0] < 13_000, eng.calls
    assert "Thanks" not in r.raw_text
