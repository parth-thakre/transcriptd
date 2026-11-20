import numpy as np
import pytest

from transcriptd.asr.base import Transcript, TranscribeRequest, Word
from transcriptd.asr.registry import EngineRegistry
from transcriptd.config import Settings
from transcriptd.db import Database, VocabTerm
from transcriptd.pipeline import Pipeline


class FakeEngine:
    name = "fake"
    model_name = "fake"

    def __init__(self, words, scored=True):
        self._words, self._scored = words, scored

    def transcribe(self, req):
        return Transcript(words=list(self._words), language="en", scored=self._scored, engine="fake", model="fake")

    def supports_streaming(self):
        return False

    def close(self):
        pass


def _w(text, conf=0.9):
    return [Word(t, i * 100, i * 100 + 90, conf) for i, t in enumerate(text.split())]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    # Before Settings.load: it creates the data and models dirs and writes the token, and
    # must never do that in whatever directory the tests run from.
    monkeypatch.setenv("TRANSCRIPTD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRANSCRIPTD_MODELS_DIR", str(tmp_path / "models"))
    s = Settings.load(tmp_path / "nope.toml")
    assert s.data_dir == tmp_path
    db = Database(tmp_path / "t.db")
    reg = EngineRegistry(s)
    p = Pipeline(s, reg, db)
    return p, reg, db


def test_the_fixture_writes_only_to_tmp(pipeline, tmp_path):
    p, _, _ = pipeline
    assert p.settings.data_dir == tmp_path and (tmp_path / "token").exists()
    assert p.settings.models_dir.is_relative_to(tmp_path)


def _install(reg, engine):
    reg._engines[reg.settings.default_profile] = engine


def test_end_to_end_keeps_negation_and_reports_edits(pipeline):
    p, reg, _ = pipeline
    _install(reg, FakeEngine(_w("um never merge merge this before friday")))
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.text == "Never merge this before friday"
    assert r.raw_text == "um never merge merge this before friday"
    reasons = sorted(e.reason for e in r.edits)
    assert reasons == ["filler", "stutter"]
    assert all(e.accepted for e in r.edits)


def test_vocab_rule_flows_through_guard(pipeline):
    p, reg, db = pipeline
    with db.session() as s:
        t = VocabTerm(term="Kubernetes")
        t.heard_as = ["cooper netties"]
        s.add(t)
        s.commit()
    p.reload_vocab()
    _install(reg, FakeEngine(_w("deploy it to cooper netties")))
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.text == "Deploy it to Kubernetes"
    assert r.replacements[0].meant == "Kubernetes"


def test_low_confidence_words_are_flagged(pipeline):
    p, reg, _ = pipeline
    words = _w("send it to sam")
    words[-1].conf = 0.3
    _install(reg, FakeEngine(words))
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.low_confidence == [{"index": 3, "text": "sam", "conf": 0.3}]


def test_unscored_engine_flags_nothing(pipeline):
    p, reg, _ = pipeline
    _install(reg, FakeEngine(_w("send it", conf=0.0), scored=False))
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.low_confidence == []
    assert r.scored is False


def test_quiet_input_is_boosted_before_the_engine(pipeline):
    from transcriptd.audio import normalize_gain, rms_db, trim_trailing_silence

    rng = np.random.default_rng(1)
    whisper = (rng.standard_normal(16000) * 0.003).astype(np.float32)  # about -50 dBFS
    assert rms_db(whisper) < -45
    boosted, gain = normalize_gain(whisper)
    assert 15 < gain <= 30 and -26 < rms_db(boosted) < -18
    loud = (rng.standard_normal(16000) * 0.1).astype(np.float32)
    assert normalize_gain(loud)[1] == 0.0
    # a whisper-level tail is no longer trimmed away as silence
    assert len(trim_trailing_silence(whisper)) > 14000
    p, reg, _ = pipeline
    _install(reg, FakeEngine(_w("do not merge")))
    r = p.process(whisper)
    assert r.timings_ms.get("gain_db", 0) > 15 and r.text == "Do not merge"


def test_llm_cleanup_is_guarded(pipeline):
    p, reg, _ = pipeline
    p.settings.cleanup.llm = True
    p.settings.llm.base_url = "http://fake"
    p.settings.llm.model = "fake"
    _install(reg, FakeEngine(_w("we will not ship twenty five units before friday")))
    # A helpful reformat is kept...
    p.rewriter = lambda t: "We will not ship 25 units before Friday."
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.text == "We will not ship 25 units before Friday."
    # ...a meaning change is reverted, even from a "cleanup" model.
    p.rewriter = lambda t: "We will ship twenty five units after Friday."
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.text == "We will not ship twenty five units before Friday."
    assert [e.reason for e in r.edits if not e.accepted] == ["protected word deleted", "protected word changed"]
    # LLM failure never blocks the dictation.
    def boom(_):
        raise RuntimeError("down")
    p.rewriter = boom
    r = p.process(np.zeros(16000, dtype=np.float32))
    assert r.text == "We will not ship twenty five units before friday"
    assert "llm" in r.timings_ms


def test_llm_cleanup_never_changes_a_decimal_or_a_sign(pipeline):
    p, _, _ = pipeline
    p.settings.cleanup.llm = True
    p.settings.llm.base_url = "http://fake"
    p.rewriter = lambda t, lists=True: "Take 15 units at 5 degrees."
    r = _finish(p, "take 1.5 units at -5 degrees")
    assert r.text == "Take 1.5 units at -5 degrees."
    assert sorted(e.raw for e in r.edits if not e.accepted) == ["-5", "1.5"]


def test_a_long_ramble_goes_to_the_bigger_model_and_falls_back(pipeline, monkeypatch, tmp_path):
    p, _, _ = pipeline
    p.settings.llm.base_url, p.settings.llm.model = "http://small", "small"
    key = tmp_path / "key"
    key.write_text("sk-test\n")
    p.settings.llm_long = p.settings.llm_long.model_copy(update={"base_url": "http://big", "model": "big",
                                                                 "api_key_file": str(key)})
    calls = []

    def rewrite(cfg, text, *a, **k):
        calls.append(cfg.model)
        if cfg.model == "big" and "down" in text:
            raise RuntimeError("timed out")
        return f"{cfg.model}: {text}"

    monkeypatch.setattr("transcriptd.pipeline.llm.rewrite", rewrite)
    assert p.rewriter("a short take") == "small: a short take" and calls == ["small"]
    calls.clear()
    # A long take asks both at once: the big model's answer wins...
    assert p.rewriter("word " * 40).startswith("big:")
    # ...and the small one's stands in when it fails.
    assert p.rewriter("down " * 40).startswith("small:")


def test_a_slow_long_model_costs_its_budget_not_the_budget_plus_the_small_model(pipeline, monkeypatch):
    import time as _time

    p, _, _ = pipeline
    p.settings.llm.base_url, p.settings.llm.model = "http://small", "small"
    p.settings.llm_long = p.settings.llm_long.model_copy(update={"base_url": "http://big", "model": "big"})
    p.settings.cleanup.llm_long_timeout_s = 0.3

    def rewrite(cfg, text, *a, **k):
        _time.sleep(2.0 if cfg.model == "big" else 0.2)
        return f"{cfg.model}: {text}"

    monkeypatch.setattr("transcriptd.pipeline.llm.rewrite", rewrite)
    t0 = _time.perf_counter()
    assert p.rewriter("word " * 40).startswith("small:")
    assert _time.perf_counter() - t0 < 0.6  # the small model ran alongside, not after


def test_an_llm_key_file_is_read_at_each_call(tmp_path):
    from transcriptd import llm
    from transcriptd.config import LLMConfig
    key = tmp_path / "key"
    key.write_text("sk-one\n")
    cfg = LLMConfig(base_url="http://x", model="m", api_key_file=str(key))
    assert llm._headers(cfg)["Authorization"] == "Bearer sk-one"
    key.write_text("sk-two")
    assert llm._headers(cfg)["Authorization"] == "Bearer sk-two"


def test_llm_ramble_cuts_are_on_by_default_and_checked(pipeline):
    p, reg, _ = pipeline
    p.settings.cleanup.llm = True
    p.settings.llm.base_url = "http://fake"
    p.settings.llm.model = "fake"
    _install(reg, FakeEngine(_w("okay so I was gonna, I want you to not deploy before friday")))
    p.rewriter = lambda t: "I want you to deploy on Friday."
    r = p.process(np.zeros(16000, dtype=np.float32))
    # The tic and the false start go; "not" and "before" come back.
    assert r.text == "I want you to not deploy before Friday."
    assert [e.reason for e in r.edits if e.accepted] == ["verbal tic + false start"]
    p.settings.cleanup.cuts = False
    p.rewriter = lambda t: "I want you to not deploy before Friday."
    assert p.process(np.zeros(16000, dtype=np.float32)).text.startswith("okay so I was gonna, I want")


def test_chat_audio_engine_sends_base64_wav_and_spreads_words():
    import httpx

    from transcriptd.asr.chat_audio import ChatAudioEngine

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        seen["parts"] = body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "\"never merge this\""}}]})

    e = ChatAudioEngine("http://fake/v1", "", "gemma4")
    e._client = httpx.Client(transport=httpx.MockTransport(handler))
    t = e.transcribe(TranscribeRequest(pcm=np.zeros(16000 * 3, dtype=np.float32), language="en"))
    assert [w.text for w in t.words] == ["never", "merge", "this"]
    assert t.scored is False and t.words[-1].end_ms == 3000
    assert seen["parts"][1]["type"] == "input_audio" and seen["parts"][1]["input_audio"]["format"] == "wav"
    assert "language code 'en'" in seen["parts"][0]["text"]


def test_context_document_vocab_and_smart_join(pipeline):
    from transcriptd.context import DictationContext, document_terms, smart_join

    ctx = DictationContext(app="Code", before="Ping Sanjay about the Hyprland config and", after="")
    assert set(document_terms(ctx)) == {"Sanjay", "Hyprland"}
    p, reg, _ = pipeline
    words = _w("tell sanjai to check hyper land today", conf=0.7)
    _install(reg, FakeEngine(words))
    r = p.process(np.zeros(16000, dtype=np.float32), context=ctx)
    assert r.text == "Tell Sanjay to check Hyprland today"
    # mid-sentence: lowercase first letter, leading space, trailing space at end of text
    assert r.insert_text == " tell Sanjay to check Hyprland today "
    # sentence start: keep the capital, no leading space
    r2 = p.process(np.zeros(16000, dtype=np.float32), context=DictationContext(before="Ask Sanjay about Hyprland. ", after=""))
    assert r2.insert_text == "Tell Sanjay to check Hyprland today "
    # gluing before a word: trailing space, and no leading space after an opening quote
    # after an opening quote we keep the capital (a quoted sentence), add the trailing space
    assert smart_join("Hello", DictationContext(before='say "', after='world'), "hello") == "Hello "
    assert smart_join("Hello", None) == "Hello"


def test_profile_by_script():
    from transcriptd.config import ProfileConfig
    from transcriptd.context import DictationContext, profile_for_context

    profiles = {"english": ProfileConfig(model="x", languages=["en"]), "multilingual": ProfileConfig(model="y", languages=["en", "hi", "mr"])}
    assert profile_for_context(DictationContext(before="आम्ही इथे प्रोजेक्टबद्दल बोलण्यासाठी आलो"), "english", profiles) == "multilingual"
    assert profile_for_context(DictationContext(before="plain english text here"), "english", profiles) == "english"


def test_note_style_and_restyle_reuse_raw_words_without_the_llm(pipeline):
    p, reg, _ = pipeline
    p.settings.cleanup.llm = True
    p.settings.llm.base_url = "http://fake"
    p.settings.llm.model = "fake"
    calls = []
    p.rewriter = lambda t: calls.append(t) or t
    _install(reg, FakeEngine(_w("um I need to buy milk, eggs and bread, and then call the dentist")))
    r = p.process(np.zeros(16000, dtype=np.float32), use_llm=False)
    assert r.style == "auto" and r.text == "I need to buy milk, eggs and bread, and then call the dentist" and not calls
    again = p.restyle(r.to_dict(), "note", use_llm=False)
    assert again.style == "note" and again.raw_text == r.raw_text and not calls
    assert again.text == "I need to buy:\n• milk\n• eggs\n• bread\n\nAnd then call the dentist."
    assert sorted(e.reason for e in again.edits) == ["filler", "list joiner"]
    assert p.restyle(r.to_dict(), "no-such-style").style == "auto" and calls  # config default still applies


def test_llm_punctuates_plain_text_then_the_style_lays_it_out(pipeline):
    p, reg, _ = pipeline
    p.settings.cleanup.llm = True
    p.settings.llm.base_url = "http://fake"
    p.settings.llm.model = "fake"
    seen = []
    p.rewriter = lambda t: seen.append(t) or "I need to buy milk, eggs and bread, and then call the dentist."
    _install(reg, FakeEngine(_w("um I need to buy milk eggs and bread and then call the dentist")))
    r = p.process(np.zeros(16000, dtype=np.float32), style="note")
    assert seen == ["I need to buy milk eggs and bread and then call the dentist."]  # no bullets for the LLM
    assert r.text == "I need to buy:\n• milk\n• eggs\n• bread\n\nAnd then call the dentist."
    # Paragraph breaks the LLM adds don't fight the layout.
    p.rewriter = lambda t: "I need to buy milk, eggs and bread,\n\nand then call the dentist."
    assert p.process(np.zeros(16000, dtype=np.float32), style="note").text == r.text


def test_a_note_keeps_the_list_the_llm_made(pipeline):
    p, reg, _ = pipeline
    p.settings.cleanup.llm = True
    p.settings.llm.base_url = "http://fake"
    p.settings.llm.model = "fake"
    _install(reg, FakeEngine(_w("things I need from the store eggs milk and bread")))
    p.rewriter = lambda t: "Things I need from the store:\n- eggs\n- milk\n- bread"
    r = p.process(np.zeros(16000, dtype=np.float32), style="note")
    assert r.text == "Things I need from the store:\n- eggs\n- milk\n- bread"


def _finish(p, text, **kw):
    t = Transcript(words=_w(text, conf=1.0), language="en", scored=False, engine="fake", model="fake")
    return p.finish(t, profile=p.settings.default_profile, duration_ms=1000, **kw)


def test_a_name_never_turns_a_confident_word_into_another_form_of_it(pipeline):
    from transcriptd.context import DictationContext

    p, _, db = pipeline
    with db.session() as s:
        s.add(VocabTerm(term="Zorbex"))
        s.commit()
    p.reload_vocab()
    # "Kenya" in the field, "Kenyan" said: a word of its own, not a misheard name.
    ctx = DictationContext(before="We flew out of Kenya last spring and", after="")
    assert _finish(p, "a law in the Kenyan parliament", context=ctx).text == "A law in the Kenyan parliament"
    # "zorb" is the start of the term, a word of its own when the model was sure...
    assert _finish(p, "the zorb ball is flat").text == "The zorb ball is flat"
    # ...while a misheard name still gets fixed.
    assert _finish(p, "ask zorbecks about it").text == "Ask Zorbex about it"


def _llm_on(p):
    p.settings.cleanup.llm = True
    p.settings.llm.base_url, p.settings.llm.model = "http://fake", "fake"


def test_no_lists_in_a_chat_app(pipeline):
    from transcriptd.context import DictationContext

    p, _, _ = pipeline
    _llm_on(p)
    seen = []

    def rewriter(t, lists=True):
        seen.append(lists)
        return "We need to fix:\n- the login page\n- the logs\n- the readme"

    p.rewriter = rewriter
    said = "we need to fix the login page the logs and the readme"
    chat = DictationContext.with_app(None, "com.example.whatsapp")
    r = _finish(p, said, context=chat)
    assert seen == [False] and "\n" not in r.text and r.text.startswith("We need to fix: the login page")
    assert r.insert_text == r.text  # knowing only the app changes nothing about the caret
    # A notes app keeps the list; the Message style never makes one.
    assert "\n- the logs" in _finish(p, said, context=DictationContext.with_app(None, "org.example.notes")).text
    assert "\n" not in _finish(p, said, style="message").text and seen[-1] is False
    # A restyle knows the app it was said in.
    stored = {"raw_text": said, "words": [w.to_dict() for w in _w(said)]}
    assert "\n" not in p.restyle(stored, "auto", app="com.instagram.android").text


def test_info_logs_never_carry_the_dictation_text(pipeline, caplog):
    # journald and container logs outlive a deleted dictation: at INFO and above only counts and
    # kinds, the words themselves at DEBUG.
    import logging

    p, _, _ = pipeline
    _llm_on(p)
    p.rewriter = lambda t, lists=True, ramble=False: "We will ship zanzibar crates after Friday."
    with caplog.at_level(logging.DEBUG, logger="transcriptd"):
        r = _finish(p, "we will not ship zanzibar crates before friday")
        _finish(p, "we will not ship zanzibar crates before friday", style="ramble")
    assert r.text.startswith("We will not ship")
    info = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.INFO]
    assert any("guard reverted" in m for m in info) and any("ramble rewrite rejected" in m for m in info)
    assert not any(w in m.lower() for m in info for w in ("zanzibar", "crates", "friday", "ship", "before"))
    debug = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.DEBUG]
    assert any("zanzibar" in m.lower() for m in debug)
