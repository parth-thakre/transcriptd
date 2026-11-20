"""Custom-vocabulary pass over a word-level transcript.

Two layers, both conservative:
1. Exact rules learned from corrections ("heard X" -> "meant Y"): applied whenever the
   normalized span matches.
2. Phonetic fuzzy match, two tiers:
   - a strong match (very close phonetic/romanized key) is replaced even when the ASR
     was confident, unless what it heard is a very common English word ("part",
     "path"): those only flip when the model was unsure, because "this part of the
     code" must never become "this Parth of the code";
   - a looser match is replaced only when the ASR was unsure about the span.
   Engines that cannot score words get the strong tier only.

Protected words are never touched. Every replacement is reported so the guard can
allow exactly those and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..asr.base import Word
from ..guard import PROTECTED
from ..text import normalize_key
from .phonetic import keys_for_term, phonetic_key, romanize


@dataclass(slots=True)
class VocabEntry:
    term: str
    aliases: list[str] = field(default_factory=list)
    heard_as: list[str] = field(default_factory=list)  # exact-rule sources
    keys: set[str] = field(default_factory=set)
    roman: set[str] = field(default_factory=set)  # romanized spellings, for short names
    max_words: int = 1

    @classmethod
    def build(cls, term: str, aliases: list[str] | None = None, heard_as: list[str] | None = None) -> "VocabEntry":
        aliases = aliases or []
        heard_as = [normalize_key(h) for h in (heard_as or [])]
        mw = max(len(f.split()) for f in [term, *aliases, *heard_as] if f) if (term or heard_as) else 1
        roman = {_roman_join(f) for f in [term, *aliases] if f}
        return cls(term=term, aliases=aliases, heard_as=heard_as, keys=keys_for_term(term, aliases),
                   roman=roman, max_words=max(mw, 1))


def _roman_join(text: str) -> str:
    return "".join(romanize(normalize_key(t)) for t in text.split())


@dataclass(slots=True)
class Replacement:
    start: int  # word index (inclusive)
    end: int  # word index (exclusive)
    heard: str
    meant: str
    score: float
    rule: str  # "exact" | "phonetic"

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "heard": self.heard, "meant": self.meant, "score": round(self.score, 3), "rule": self.rule}


# Very frequent English words that also happen to sound like names/terms. A confident
# hit on one of these is left alone; the correction loop can still teach an exact rule.
COMMON_WORDS: frozenset[str] = frozenset("""
the be to of and a in that have i it for not on with he as you do at this but his by from they
we say her she or an will my one all would there their what so up out if about who get which go
me when make can like time no just him know take people into year your good some could them see
other than then now look only come its over think also back after use two how our work first well
way even new want because any these give day most us is are was were been has had did does am
part parts path paths port ports party pass past cost post most host lost best test rest west
mark march mart cart card art heart hard bar bare bear car care far fair fare star start
where here there near dear deer door more store score sure pure cure poor pour four for fore
book look cook took hook cool tool pool fool full pull push bush rush cash catch match batch
line fine mine nine pine sign sine wine vine time lime dime chime climb prime crime
man men main mean mind mile file pile tile style while smile
sun son run fun gun bun done none tone bone phone zone stone alone own home come some
ran rain rate late gate date hate mate state great grade trade
call tall wall fall ball hall small mall all
bit bite bet but bat beat bead lead read red rid ride wide side site sight light night right
write white wait way weigh pay day say may play stay
word world work worth war warm ward award toward
key keys kite kind king kid kids
ten tin tan ton turn torn tour tower
""".split())


@dataclass(slots=True)
class VocabMatcher:
    entries: list[VocabEntry]
    phonetic_threshold: float = 0.86
    max_conf_for_replacement: float = 0.85
    strong_threshold: float = 0.88
    longer_span_margin: float = 0.05  # a longer span must beat a shorter match by this much

    def _score(self, heard: str, span_keys: list[str], e: VocabEntry) -> float:
        return _entry_score(heard, span_keys, e)

    def apply(self, words: list[Word], scored: bool = True) -> tuple[list[Word], list[Replacement]]:
        if not self.entries or not words:
            return words, []
        max_n = min(max(e.max_words for e in self.entries) + 1, 4)
        keys = [normalize_key(w.text) for w in words]
        n = len(words)
        reps: list[Replacement] = []
        i = 0
        while i < n:
            best: Replacement | None = None
            # Shortest span first; a longer span only wins if it is clearly better, so
            # "to hyperland" can never swallow "to" when "hyperland" alone matches.
            for span in range(1, min(max_n, n - i) + 1):
                j = i + span
                span_keys = keys[i:j]
                if any(k in PROTECTED for k in span_keys):
                    break  # a protected word can be neither replaced nor absorbed
                heard = " ".join(span_keys)
                exact = False
                for e in self.entries:
                    if heard == normalize_key(e.term) or heard in (normalize_key(a) for a in e.aliases):
                        continue  # already right; do not "fix" it
                    if heard in e.heard_as:
                        best = Replacement(i, j, heard, e.term, 1.0, "exact")
                        exact = True
                        break
                    score = self._score(heard, span_keys, e)
                    if score <= 0.0:
                        continue
                    unsure = scored and min(w.conf for w in words[i:j]) <= self.max_conf_for_replacement
                    common = all(k in COMMON_WORDS for k in span_keys) or _variant(heard, e)
                    strong = score >= self.strong_threshold and not common
                    loose = scored and score >= self.phonetic_threshold and unsure
                    if not (strong or loose):
                        continue
                    if span > 1:
                        # Must clearly beat the best single word inside the span, or we
                        # would swallow a neighbour ("to hyperland" -> "Hyprland").
                        inner = max(self._score(k, [k], e) for k in span_keys)
                        if score <= inner + self.longer_span_margin:
                            continue
                    if best is None or score > best.score:
                        best = Replacement(i, j, heard, e.term, score, "phonetic")
                if exact:
                    break
            if best is not None:
                reps.append(best)
                i = best.end
            else:
                i += 1

        if not reps:
            return words, []
        out: list[Word] = []
        idx = 0
        for r in reps:
            out.extend(words[idx : r.start])
            span = words[r.start : r.end]
            trailing = _trailing_punct(span[-1].text)
            out.append(Word(text=r.meant + trailing, start_ms=span[0].start_ms, end_ms=span[-1].end_ms,
                            conf=max(w.conf for w in span)))
            idx = r.end
        out.extend(words[idx:])
        return out, reps


# Endings that make another word of the same stem: "India" -> "Indian", "Codex" is not "code".
_INFLECTIONS = ("s", "es", "'s", "n", "an", "ian", "ns", "ans", "ese", "ed", "d", "ing", "er", "ers", "ism", "ist",
                "ic", "al", "ly", "y", "ish")


def _variant(heard: str, e: VocabEntry) -> bool:
    """`heard` is a word of its own built on the term ("Indian" for India) or the start of it
    ("code" for Codex): a confident model meant that word, so like a common word it's only
    replaced when the model was unsure."""
    for f in (e.term, *e.aliases):
        t = normalize_key(f)
        short, long_ = sorted((heard, t), key=len)
        if len(short) < 3 or short == long_ or not long_.startswith(short):
            continue
        if short == heard or long_[len(short):] in _INFLECTIONS:
            return True
    return False


def _entry_score(heard: str, span_keys: list[str], e: VocabEntry) -> float:
    pk = phonetic_key(heard)
    if not pk:
        return 0.0
    # Digits are never a sound-alike: "i7" is not the term "i5", "4060" is not "4070".
    if (_digits(heard) or _digits(e.term)) and _digits(heard) not in (
            _digits(f) for f in (e.term, *e.aliases)):
        return 0.0
    score = max((fuzz.ratio(pk, k) / 100.0 for k in e.keys), default=0.0)
    score = max(score, max((fuzz.ratio(pk.replace(" ", ""), k) / 100.0 for k in e.keys), default=0.0))
    rj = _roman_join(heard)
    return max(score, max((fuzz.ratio(rj, r) / 100.0 for r in e.roman), default=0.0))


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _trailing_punct(s: str) -> str:
    i = len(s)
    while i > 0 and s[i - 1] in ",.?!;:।":
        i -= 1
    return s[i:]


def replacement_pairs(reps: list[Replacement]) -> frozenset[tuple[str, str]]:
    """(heard_key, meant_key) pairs in the form the guard expects."""
    return frozenset((r.heard, normalize_key(r.meant)) for r in reps)
