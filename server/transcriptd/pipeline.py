"""ASR -> vocab -> cleanup -> guard. The one place that decides what text goes out."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

import numpy as np

from .asr.base import Engine, Transcript, TranscribeRequest, Word
from .asr.chunking import SpeechSpans, needs_vad, transcribe_long
from .audio import normalize_gain
from . import learn, llm, ramble
from .asr.registry import EngineRegistry
from .cleanup import CleanupOptions, cleanup, format_layout
from .config import Settings, StyleConfig
from .context import DictationContext, document_terms, is_chat_app, smart_join
from .db import Database
from .guard import Edit, GuardPolicy, guard, unlist
from .text import normalize_key
from .vocab.matcher import Replacement, VocabEntry, VocabMatcher, replacement_pairs

log = logging.getLogger(__name__)

LOW_CONF = 0.6
_LIST = re.compile(r"(?m)^\s*(?:[-•*]|\d{1,2}[.)])\s+\S")


@dataclass(slots=True)
class DictationResult:
    raw_text: str
    text: str
    words: list[Word]
    edits: list[Edit]
    replacements: list[Replacement]
    low_confidence: list[dict]
    insert_text: str  # text fitted to the caret context (spacing/case); equals text when no context
    language: str | None
    profile: str
    engine: str
    model: str
    scored: bool
    duration_ms: int
    timings_ms: dict[str, float] = field(default_factory=dict)
    style: str = "auto"
    members: dict[str, str] = field(default_factory=dict)  # ensemble: each member's raw text
    # Ramble style: {"used": True} when the text is the checked rewrite, or {"used": False,
    # "problems": [...]} when the check failed and the text is the usual guarded cleanup.
    rewrite: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "raw_text": self.raw_text,
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
            "edits": [
                {"kind": e.kind, "raw": e.raw, "cleaned": e.cleaned, "accepted": e.accepted,
                 "reason": e.reason, "raw_span": list(e.raw_span)}
                for e in self.edits
            ],
            "replacements": [r.to_dict() for r in self.replacements],
            "low_confidence": self.low_confidence,
            "insert_text": self.insert_text,
            "language": self.language,
            "profile": self.profile,
            "engine": self.engine,
            "model": self.model,
            "scored": self.scored,
            "duration_ms": self.duration_ms,
            "timings_ms": self.timings_ms,
            "style": self.style,
        }
        if self.members:
            d["members"] = self.members
        if self.rewrite is not None:
            d["rewrite"] = self.rewrite
        return d


class Pipeline:
    def __init__(self, settings: Settings, registry: EngineRegistry, db: Database):
        self.settings = settings
        self.registry = registry
        self.db = db
        self._matcher: VocabMatcher | None = None
        self._matcher_lock = threading.Lock()
        self._suppressed: set[str] = set()
        self.speech_spans = SpeechSpans(settings.models_dir)  # VAD for long takes; swapped out in tests
        self._learned: tuple[learn.Preferences, list[tuple[str, str]]] | None = None

    # -- vocab cache -----------------------------------------------------------------

    def reload_vocab(self) -> None:
        """Read the vocabulary and suppressions and install them, as one step under the lock: a
        reload that starts after a commit installs what it committed, and no reload that read
        before it (a correction's, paused) can install its older read over it."""
        vc = self.settings.vocab
        with self._matcher_lock:
            entries = [VocabEntry.build(t.term, t.aliases, t.heard_as) for t in self.db.vocab_terms()]
            self._matcher = VocabMatcher(entries, vc.phonetic_threshold, vc.max_conf_for_replacement, vc.strong_threshold)
            self._suppressed = self.db.suppressed_keys()
        log.info("vocab loaded: %d terms", len(entries))

    def learned(self) -> tuple[learn.Preferences, list[tuple[str, str]]]:
        """What the user's edits so far teach the LLM pass (learn.py), cached until the next edit."""
        if self._learned is None:
            try:
                rows = learn.ramble_rows(self.db)
                prefs = learn.preferences([(r, t, e) for r, t, e, _ in rows])
                ex = learn.examples([(r, t, e) for r, t, e, style in rows if style == "ramble"])
            except Exception as exc:  # learning is a bonus; never block a dictation on it
                log.warning("could not load learned edits: %s", exc)
                prefs, ex = learn.Preferences(), []
            self._learned = (prefs, ex)
        return self._learned

    def forget_learned(self) -> None:
        self._learned = None

    def rewriter(self, text: str, lists: bool = True, ramble: bool = False) -> str:
        """LLM formatting pass; swapped out in tests. A long ramble goes to the bigger model if
        there is one, with the usual one running alongside: its answer stands in if the bigger
        one fails or takes longer than llm_long_timeout_s (per 150 words), so a slow cloud call
        never adds the small model's time on top."""
        c = self.settings.cleanup
        n = len(text.split())
        prefs, mine = self.learned()
        extra = {"hints": prefs.hints(), "examples": mine if ramble else None}

        def small() -> str:
            return llm.rewrite(self.settings.llm, text, c.llm_instructions, c.llm_model, c.llm_timeout_s, cuts=c.cuts,
                               lists=lists, ramble=ramble, **extra)

        if not (llm.configured(self.settings.llm_long) and n >= c.llm_long_words):
            return small()
        budget = c.llm_long_timeout_s * max(1.0, n / 150)
        pool = ThreadPoolExecutor(2, thread_name_prefix="llm")
        try:
            big = pool.submit(llm.rewrite, self.settings.llm_long, text, c.llm_instructions, None, budget, cuts=c.cuts,
                              lists=lists, ramble=ramble, **extra)
            backup = pool.submit(small) if self.settings.llm.base_url else None
            try:
                return big.result(timeout=budget)
            except Exception as exc:  # the bigger model is a bonus; the small one is already on it
                if backup is None:
                    raise
                log.warning("long-take llm %s failed or slow, using %s: %s", self.settings.llm_long.model,
                            self.settings.llm.model, exc or "timed out")
                return backup.result()
        finally:
            pool.shutdown(wait=False)

    def style(self, name: str | None) -> tuple[str, StyleConfig]:
        styles = self.settings.cleanup.styles
        if name and name in styles:
            return name, styles[name]
        if "auto" in styles:
            return "auto", styles["auto"]
        return "auto", StyleConfig(label="Auto")

    def lists_ok(self, sty: StyleConfig, app: str | None) -> bool:
        """May the LLM make lists: the style says, or else yes, except in a chat app."""
        if sty.lists is not None:
            return sty.lists
        return not is_chat_app(app, self.settings.cleanup.chat_apps)

    def matcher(self) -> VocabMatcher:
        if self._matcher is None:
            self.reload_vocab()
        return self._matcher  # type: ignore[return-value]

    def prompt_terms(self) -> list[str]:
        m = self.matcher()
        return [e.term for e in m.entries[: self.settings.vocab.prompt_terms_max]]

    # -- the pipeline ------------------------------------------------------------------

    def process(
        self,
        pcm: np.ndarray,
        *,
        profile: str | None = None,
        language: str | None = None,
        cleanup_opts: CleanupOptions | None = None,
        transcript: Transcript | None = None,
        context: DictationContext | None = None,
        style: str | None = None,
        use_llm: bool | None = None,
    ) -> DictationResult:
        name, prof = self.registry.profile(profile)
        engine: Engine = self.registry.get(name)
        duration_ms = int(len(pcm) * 1000 / 16000)
        pcm, gain_db = normalize_gain(pcm)
        if gain_db:
            log.debug("quiet input: applied %.1f dB gain", gain_db)
        if transcript is None:
            req = TranscribeRequest(pcm=pcm, language=language or prof.language, prompt_terms=self.prompt_terms())
            spans = self.speech_spans(pcm) if needs_vad(engine, len(pcm)) else None
            transcript = engine.transcribe(req) if spans is None else transcribe_long(engine, req, spans)
        if gain_db:
            transcript.timings_ms["gain_db"] = round(gain_db, 1)
        return self.finish(transcript, profile=name, duration_ms=duration_ms, cleanup_opts=cleanup_opts,
                           context=context, style=style, use_llm=use_llm)

    def restyle(self, stored: dict, style: str | None, use_llm: bool | None = None,
                app: str | None = None) -> DictationResult:
        """Re-run everything after ASR on a stored result's raw words with another style (`app`:
        the dictation's app hint)."""
        transcript = Transcript(
            words=[Word(w["text"], w["start_ms"], w["end_ms"], w.get("conf", 1.0)) for w in stored.get("words", [])],
            language=stored.get("language"), scored=stored.get("scored", True),
            engine=stored.get("engine", ""), model=stored.get("model", ""),
        )
        if not transcript.words and stored.get("raw_text"):  # results from engines without word lists
            transcript.words = [Word(t, 0, 0, 1.0) for t in stored["raw_text"].split()]
        return self.finish(transcript, profile=stored.get("profile") or self.settings.default_profile,
                           duration_ms=int(stored.get("duration_ms") or 0), style=style, use_llm=use_llm,
                           context=DictationContext.with_app(None, app))

    def finish(
        self,
        transcript: Transcript,
        *,
        profile: str,
        duration_ms: int,
        cleanup_opts: CleanupOptions | None = None,
        context: DictationContext | None = None,
        style: str | None = None,
        use_llm: bool | None = None,
    ) -> DictationResult:
        """vocab -> cleanup -> (LLM) -> layout -> guard on an existing transcript. `use_llm` overrides
        [cleanup] llm for this call (the phone app sends its own setting)."""
        style_name, sty = self.style(style)
        raw_words = transcript.words
        raw_text = transcript.text

        matcher = self.matcher()
        if context is not None:
            doc = [t for t in document_terms(context) if normalize_key(t) not in {normalize_key(e.term) for e in matcher.entries}]
            if doc:
                matcher = VocabMatcher([*matcher.entries, *(VocabEntry.build(t) for t in doc)], matcher.phonetic_threshold,
                                       matcher.max_conf_for_replacement, matcher.strong_threshold)
        words, reps = matcher.apply(raw_words, scored=transcript.scored)
        vocab_text = " ".join(w.text for w in words)

        opts = cleanup_opts or CleanupOptions(
            remove_fillers=self.settings.cleanup.remove_fillers,
            collapse_stutters=self.settings.cleanup.collapse_stutters,
            capitalize_first=self.settings.cleanup.capitalize_first,
            ensure_terminal_punctuation=self.settings.cleanup.ensure_terminal_punctuation
            if sty.terminal_punctuation is None else sty.terminal_punctuation,
            layout=sty.layout,
        )
        # The LLM sees plain text: the style's layout (bullets, email paragraphs) is applied after
        # it, on its better punctuation, so the LLM never has to preserve or invent structure.
        cleaned = cleanup(vocab_text, replace(opts, layout="none"))
        llm_ms = None
        lists = self.lists_ok(sty, context.app if context else None)
        llm_on = (self.settings.cleanup.llm if use_llm is None else use_llm) and bool(self.settings.llm.base_url)
        rewrite_info = None
        if sty.rewrite and llm_on and raw_words:
            # Ramble: a free rewrite, checked for facts (ramble.check) instead of word by word.
            t0 = time.perf_counter()
            try:
                out = self.rewriter(cleaned, lists=lists, ramble=True)
                if not lists and _LIST.search(out):
                    out = unlist(out)
                problems = ramble.check(vocab_text, out)
            except Exception as exc:  # the rewrite is optional; fall back to the guarded cleanup
                out, problems = "", [f"rewrite failed: {exc}"]
            ramble_ms = round((time.perf_counter() - t0) * 1000, 1)
            if not problems:
                return self._result(transcript, profile, duration_ms, style_name, context, out, [], reps,
                                    {**transcript.timings_ms, "llm": ramble_ms}, {"used": True})
            # The problems quote the take: text goes to DEBUG only, never to INFO logs (journald and
            # container logs outlive a deleted dictation).
            log.info("ramble rewrite rejected (%d problems), using the guarded cleanup", len(problems))
            log.debug("ramble rewrite problems: %s", "; ".join(problems))
            rewrite_info = {"used": False, "problems": problems, "rejected": out}
        if llm_on:
            t0 = time.perf_counter()
            try:
                cleaned = self.rewriter(cleaned) if lists else self.rewriter(cleaned, lists=False)
            except Exception as exc:  # the LLM is optional; never block a dictation on it
                log.warning("llm cleanup skipped: %s", exc)
            llm_ms = round((time.perf_counter() - t0) * 1000, 1)
            if not lists and _LIST.search(cleaned):  # a chat message is sentences, whatever the LLM did
                cleaned = unlist(cleaned)
        # A note keeps a list the LLM already made; otherwise the style lays the text out.
        if opts.layout != "none" and not (opts.layout == "note" and _LIST.search(cleaned)):
            cleaned = format_layout(" ".join(cleaned.split()), opts.layout)

        policy = GuardPolicy(
            allow_filler_deletion=opts.remove_fillers,
            allow_stutter_collapse=opts.collapse_stutters,
            allow_cuts=self.settings.cleanup.cuts,
            vocab_replacements=replacement_pairs(reps),
            extra_protected=frozenset(normalize_key(w) for w in self.settings.cleanup.extra_protected),
        )
        g = guard(raw_text, cleaned, policy)
        if g.rejected:  # counts and kinds only at INFO; the words themselves at DEBUG
            log.info("guard reverted %d edits (%s)", len(g.rejected), ", ".join(sorted({e.kind for e in g.rejected})))
        for e in g.rejected:
            log.debug("guard reverted %s: %r -> %r (%s)", e.kind, e.raw, e.cleaned, e.reason)

        return self._result(transcript, profile, duration_ms, style_name, context, g.text, g.edits, reps,
                            {**transcript.timings_ms, **({"llm": llm_ms} if llm_ms is not None else {})},
                            rewrite_info)

    def _result(self, transcript: Transcript, profile: str, duration_ms: int, style_name: str,
                context: DictationContext | None, text: str, edits: list[Edit], reps: list[Replacement],
                timings: dict[str, float], rewrite: dict | None) -> DictationResult:
        raw_words = transcript.words
        low = []
        if transcript.scored:
            for i, w in enumerate(raw_words):
                k = normalize_key(w.text)
                if w.conf < LOW_CONF and k and k not in self._suppressed:
                    low.append({"index": i, "text": w.text, "conf": round(w.conf, 3)})
        return DictationResult(
            raw_text=transcript.text,
            text=text,
            insert_text=smart_join(text, context, raw_words[0].text if raw_words else None),
            words=raw_words,
            edits=edits,
            replacements=reps,
            low_confidence=low,
            language=transcript.language,
            profile=profile,
            engine=transcript.engine,
            model=transcript.model,
            scored=transcript.scored,
            duration_ms=duration_ms,
            timings_ms=timings,
            style=style_name,
            members=transcript.members,
            rewrite=rewrite,
        )
