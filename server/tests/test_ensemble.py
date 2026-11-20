"""ROVER ensemble (asr/ensemble.py): fake engines, invented sentences."""

import threading
from dataclasses import replace

import numpy as np
import pytest
from pydantic import ValidationError

from tests.test_pipeline import pipeline  # noqa: F401  (fixture)
from transcriptd.asr.base import TranscribeRequest, Transcript, Word
from transcriptd.asr.ensemble import EnsembleEngine, spoken_keys
from transcriptd.asr.registry import EngineRegistry
from transcriptd.config import ProfileConfig, Settings


class Fake:
    """A member engine that always says `text`. quality: 2 = real word times, 0 = none."""

    name = "fake"

    def __init__(self, text, *, conf=1.0, scored=False, quality=0, fail=False, model="fake", window_ms=None):
        self.text, self.conf, self.scored, self.fail = text, conf, scored, fail
        self.word_time_quality = quality
        self.model_name = model
        self.window_ms = window_ms
        self.calls = 0

    def transcribe(self, req):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model fell over")
        conf = self.conf if isinstance(self.conf, list) else [self.conf] * len(self.text.split())
        base = 1000 if self.word_time_quality else 0
        words = [Word(t, base + i * 300, base + i * 300 + 250, c) for i, (t, c) in enumerate(zip(self.text.split(), conf))]
        return Transcript(words=words, language="en", scored=self.scored, engine="fake", model=self.model_name,
                          timings_ms={"wall": 1.0})

    def supports_streaming(self):
        return False

    def close(self):
        pass


def _run(*texts, **kw):
    members = [(f"m{i}", t if isinstance(t, Fake) else Fake(t)) for i, t in enumerate(texts)]
    return EnsembleEngine(members, **kw).transcribe(TranscribeRequest(pcm=np.zeros(16000, np.float32), language="en"))


def test_spoken_keys():
    assert spoken_keys("2026") == ["twenty", "twenty", "six"]
    assert spoken_keys("226,") == ["two", "hundred", "twenty", "six"]
    assert spoken_keys("Uh,") == ["um"]
    assert spoken_keys("GPT-4") == ["gpt", "four"]
    assert spoken_keys("...") == []
    # Number punctuation is said, not dropped: "1.5" is not fifteen, "3:30" not three hundred thirty.
    assert spoken_keys("1.5") == ["one", "point", "five"]
    assert spoken_keys("3:30") == ["three", "thirty"]
    assert spoken_keys("1,500") == ["one", "thousand", "five", "hundred"]
    assert spoken_keys("$5.") == ["five"]


def test_two_against_one_substitution_wins():
    t = _run("the parcel arrives on thursday", "the parcel arrived on thursday", "the parcel arrived on thursday")
    assert t.text == "the parcel arrived on thursday"


def test_a_three_way_tie_goes_to_the_pivot():
    t = _run("we packed the green tent", "we packed the clean tent", "we packed the queen tent")
    assert t.text == "we packed the green tent"


def test_a_two_way_tie_goes_to_the_pivot():
    # Only two members active: one against one, the pivot's word stays.
    t = _run("bring the blue folder", "bring the true folder", "")
    assert t.text == "bring the blue folder"


def test_an_empty_member_abstains_instead_of_voting_everything_away():
    # Counted as a vote for nothing, the empty member and the one that missed "Oskar" would
    # delete it 2 to 1.
    t = _run("good morning dear Oskar", "", "good morning dear")
    assert t.text == "good morning dear Oskar"
    assert t.members == {"m0": "good morning dear Oskar", "m1": "", "m2": "good morning dear"}


def test_an_empty_pivot_abstains_too():
    # Voting, the empty pivot would tie 1-1 with the only member that heard speech and win.
    t = _run("", "do not deploy", Fake("x", fail=True))
    assert t.text == "do not deploy"
    assert _run("", "", "").words == []


def test_number_words_everyone_agreed_on_stay_with_the_number():
    # "hundred twenty" is shared by all three; voting the keys either side of it separately
    # builds 223, which nobody said.
    t = _run("pay 123 dollars", "pay 224 dollars", "pay 233 dollars")
    assert t.text == "pay 123 dollars"
    t = _run("pay 124 dollars", "pay 123 dollars", "pay 123 dollars")
    assert t.text == "pay 123 dollars"
    t = _run("pay one hundred and twenty three dollars", "pay two hundred and twenty four dollars",
             "pay two hundred and thirty three dollars")
    assert t.text == "pay one hundred and twenty three dollars"
    # "and" between two numbers joins nothing: each is voted on its own, 2-1 each.
    t = _run("buy 2 and 4 more", "buy 2 and 3 more", "buy 5 and 3 more")
    assert t.text == "buy 2 and 3 more"
    t = _run("buy two and four more", "buy two and three more", "buy five and three more")
    assert t.text == "buy two and three more"


def test_majority_deletion_and_insertion():
    t = _run("so the train was late", "the train was late", "the train was late")
    assert t.text == "the train was late"
    t = _run("the train was late", "the train was very late", "the train was very late")
    assert t.text == "the train was very late"


def test_a_number_region_is_taken_whole_not_stitched():
    # Slot by slot, "hundred" (pivot) + "twenty" (2 votes) + "six" would read
    # "hundred twenty six", a number nobody said.
    t = _run("in the year hundred and six it rained",
             "in the year twenty twenty six it rained",
             "in the year 226 it rained")
    assert t.text == "in the year hundred and six it rained"
    assert "hundred twenty six" not in t.text


def test_a_number_region_goes_to_the_majority_span_with_its_display_form():
    t = _run("we need 12 chairs", "we need twelve chairs", "we need 20 chairs")
    assert t.text == "we need 12 chairs"  # pivot's spelling of the majority span
    t = _run("we need 20 chairs", "we need twelve chairs", "we need 12 chairs")
    assert t.text == "we need twelve chairs"  # P and W agree on twelve; P supplies it first


def test_a_devanagari_aside_is_kept_whole():
    # The others write fewer, different words there; slot by slot their gaps would vote most of
    # the aside away and leave a Devanagari/English mix.
    pivot = "and then she said चलो अभी घर चलते हैं okay fine"
    t = _run(pivot, "and then she said tell me okay fine", "and then she said hello okay fine")
    assert t.text == pivot


def test_no_duplicate_word_at_a_region_seam():
    t = _run("I will call the the office tomorrow", "I will call the office tomorrow", "I will call office tomorrow")
    assert t.text == "I will call the office tomorrow"
    keys = t.text.split()
    assert all(a != b for a, b in zip(keys, keys[1:]))


def test_display_words_come_verbatim_from_the_supplier():
    t = _run("Okay, let's go.", "ok lets go", "Okay. Let's go!")
    assert t.text == "Okay, let's go."  # all agree on the keys: the pivot's case and punctuation
    t = _run("it's the cafe", "it's the café,", "It's the café.")
    assert t.text == "it's the café,"


def test_a_failing_member_abstains_and_all_failing_raises():
    t = _run(Fake("lights off at ten"), Fake("", fail=True), Fake("lights off at ten"))
    assert t.text == "lights off at ten"
    assert t.members["m1"].startswith("<failed")
    t = _run(Fake("x", fail=True), Fake("the pivot fell over"), Fake("the pivot fell over"))
    assert t.text == "the pivot fell over"
    with pytest.raises(RuntimeError, match="every ensemble member failed"):
        _run(Fake("", fail=True), Fake("", fail=True))


def test_confidence_reflects_disagreement_and_the_scored_members_own_conf():
    t = _run(Fake("send the report now"),
             Fake("send the record now", scored=True, quality=2, conf=[0.9, 0.95, 0.4, 0.99]),
             Fake("send the report now"))
    assert t.scored and t.text == "send the report now"
    c = {w.text: w.conf for w in t.words}
    assert c["send"] == pytest.approx(0.9)  # all three; the scored member's own 0.9
    assert c["report"] == pytest.approx(2 / 3)  # two of three, the scored member voted otherwise
    assert c["now"] == pytest.approx(0.99)


def test_times_come_from_a_member_with_real_times():
    t = _run(Fake("water the plants"), Fake("water the plants", quality=2), Fake("water the plans"))
    assert [(w.start_ms, w.end_ms) for w in t.words] == [(1000, 1250), (1300, 1550), (1600, 1850)]
    # An inserted word nobody with real times said gets times between its neighbours.
    t = _run(Fake("water all the plants"), Fake("water the plants", quality=2), Fake("water all the plants"))
    assert t.text == "water all the plants"
    s = [(w.start_ms, w.end_ms) for w in t.words]
    assert s[0] == (1000, 1250) and s[2] == (1300, 1550)
    assert 1250 <= s[1][0] <= s[1][1] <= 1300


def test_concurrent_members_give_the_same_text():
    texts = ("the parcel arrives on thursday", "the parcel arrived on thursday", "the parcel arrived on thursday")
    assert _run(*texts, concurrent=True).text == _run(*texts).text


def test_window_is_the_smallest_member_window_and_long_form():
    e = EnsembleEngine([("a", Fake("x")), ("b", Fake("x", window_ms=30_000)), ("c", Fake("x", window_ms=None))])
    assert e.window_ms == 30_000 and e.long_form
    assert EnsembleEngine([("a", Fake("x")), ("b", Fake("x"))]).window_ms is None


# -- config and registry ------------------------------------------------------------------------

def _settings(tmp_path, **profiles):
    raw = {"profiles": {"english": {"model": "p.gguf"}, "english-qwen3": {"model": "q.gguf"},
                        "english-whisper": {"model": "w.gguf"},
                        "remote": {"engine": "openai_compat", "model": "x", "base_url": "http://h/v1"}, **profiles}}
    s = Settings.model_validate(raw)
    s.data_dir = s.models_dir = tmp_path
    return s


ENS = {"engine": "ensemble", "members": ["english-qwen3", "english", "english-whisper"], "language": "en"}


def test_ensemble_profile_validation(tmp_path):
    _settings(tmp_path, ens=ENS)
    with pytest.raises(ValidationError, match="unknown member"):
        _settings(tmp_path, ens={**ENS, "members": ["english", "nope"]})
    with pytest.raises(ValidationError, match="members must be"):
        _settings(tmp_path, ens={**ENS, "members": ["english", "remote"]})
    with pytest.raises(ValidationError, match="members must be"):
        _settings(tmp_path, ens=ENS, ens2={**ENS, "members": ["english", "ens"]})
    with pytest.raises(ValidationError, match="at least two"):
        ProfileConfig(engine="ensemble", members=["english"])
    with pytest.raises(ValidationError, match="needs a model"):
        ProfileConfig(engine="ggml")


def test_registry_builds_an_ensemble_from_member_profiles_and_reuses_loaded_ones(tmp_path, monkeypatch):
    s = _settings(tmp_path, ens=ENS)
    reg = EngineRegistry(s)
    built = []
    real = EngineRegistry._build

    def build(self, prof):
        if prof.engine == "ensemble":
            return real(self, prof)
        built.append(prof.model)
        return Fake("hello there", model=prof.model)

    monkeypatch.setattr(EngineRegistry, "_build", build)
    english = reg.get("english")  # already loaded for its own profile
    ens = reg.get("ens")
    assert isinstance(ens, EnsembleEngine)
    assert [n for n, _ in ens.members] == ["english-qwen3", "english", "english-whisper"]
    assert ens.members[1][1] is english
    assert sorted(built) == ["p.gguf", "q.gguf", "w.gguf"]  # english built once
    assert reg.get("english-whisper") is ens.members[2][1]
    assert reg.get("ens") is ens


def test_registry_builds_each_profile_once_under_concurrency(tmp_path, monkeypatch):
    s = _settings(tmp_path, ens=ENS)
    reg = EngineRegistry(s)
    built = []
    real = EngineRegistry._build
    gate = threading.Event()

    def build(self, prof):
        if prof.engine == "ensemble":
            return real(self, prof)
        gate.wait(1)
        built.append(prof.model)
        return Fake("hi", model=prof.model)

    monkeypatch.setattr(EngineRegistry, "_build", build)
    out = []
    threads = [threading.Thread(target=lambda n=n: out.append(reg.get(n))) for n in ("ens", "english", "ens", "english")]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert len(out) == 4 and sorted(built) == ["p.gguf", "q.gguf", "w.gguf"]


def test_pipeline_carries_member_texts(pipeline):  # noqa: F811
    p, reg, _ = pipeline
    reg._engines[reg.settings.default_profile] = EnsembleEngine(
        [("a", Fake("never merge before friday")), ("b", Fake("never merge before friday")),
         ("c", Fake("never merged before friday"))])
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.raw_text == "never merge before friday"
    assert r.to_dict()["members"]["c"] == "never merged before friday"
    assert "members" not in replace(r, members={}).to_dict()
