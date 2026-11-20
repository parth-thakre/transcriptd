"""The faithfulness guard.

Word-aligns the raw ASR transcript against a cleaned candidate and rejects every edit
that is not on the allowlist. The raw words are the source of truth; the cleaned text
only wins where it merely re-punctuated, re-cased, dropped a filler, applied a vocab
replacement the caller registered, or collapsed an immediate stutter.

With `allow_cuts` (ramble cleanup) it may also delete, never add or swap, spans that are
verifiably not what the speaker meant to keep: a verbal tic set off by punctuation, a
repeat or restart whose words are said again right after, a self-correction ("at 7,
actually 3"), a half-sentence they trailed off ("so what I need is... "), or an aside: a
clause or sentence the LLM judged off the point, with no negation, time word, condition,
number, name or request in it, and never more than half of what they said. Every word of
the result is still one they said, in order.

Protected tokens (negation, temporal and quantifier words in English, Hindi and Marathi)
can never be deleted or replaced, whatever the caller allows. The one exception is a cut
of soft English modal/quantity words (CUTTABLE_PROTECTED) inside a repeat or an abandoned
half-sentence; negation, time, condition words and numbers survive every cut.

Layout is not a word: line breaks and bullet markers from the cleaned text are carried
over onto the words they precede, but they can never bring a word with them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from enum import StrEnum
from itertools import pairwise

from .text import Token, detokenize, normalize_key, tokenize

# Words whose loss or substitution flips or shifts the meaning of a sentence.
# Keep this list boring and obvious; anything clever belongs in a test first.
PROTECTED: frozenset[str] = frozenset(
    normalize_key(w)
    for w in [
        # English negation / polarity
        "not", "no", "never", "none", "nothing", "nobody", "nowhere", "neither", "nor",
        "n't", "don't", "doesn't", "didn't", "won't", "wouldn't", "can't", "cannot",
        "couldn't", "shouldn't", "isn't", "aren't", "wasn't", "weren't", "hasn't",
        "haven't", "hadn't", "mustn't", "without", "unless", "except", "only", "hardly",
        "barely", "rarely", "seldom", "stop", "avoid", "refuse", "fail", "failed",
        # English temporal / ordering / conditionals
        "before", "after", "until", "till", "since", "already", "still", "yet", "again",
        "if", "whether", "provided", "providing", "assuming", "always", "sometimes", "ever", "once", "twice", "first", "last",
        "previous", "next", "instead", "rather", "but", "however", "although", "though",
        "despite", "while", "whereas",
        # English quantity / modality that changes commitments
        "all", "every", "each", "any", "some", "most", "few", "many", "must", "should",
        "may", "might", "could", "would", "will", "shall", "need", "required", "optional",
        "maybe", "probably", "definitely", "please", "do", "does", "did",
        # Hindi
        "नहीं", "ना", "मत", "कभी", "बिना", "सिर्फ", "केवल", "पहले", "बाद", "अभी", "अब",
        "फिर", "कब", "अगर", "तो", "लेकिन", "मगर", "पर", "हमेशा", "सब", "सभी", "कोई",
        "कुछ", "जरूर", "ज़रूर", "शायद", "चाहिए", "होगा", "नहि", "न",
        # Marathi
        "नाही", "नको", "नका", "कधीच", "कधी", "कधीही", "शिवाय", "फक्त", "आधी", "अगोदर",
        "नंतर", "आता", "अजून", "पुन्हा", "जर", "तर", "पण", "परंतु", "मात्र", "नेहमी",
        "सर्व", "सगळे", "प्रत्येक", "काही", "कुणी", "नक्की", "कदाचित", "पाहिजे", "हवे",
        "नये", "नव्हते", "नव्हता", "नसेल", "नसते",
    ]
)

# Protected words a ramble cut may take along with a repeat or a trailed-off half-sentence
# ("I want a plan that would... "): modality, quantity and contrast. Everything else in
# PROTECTED (negation, time, conditions, Hindi, Marathi) is never cut.
CUTTABLE_PROTECTED: frozenset[str] = frozenset(
    normalize_key(w)
    for w in [
        "must", "should", "may", "might", "could", "would", "will", "shall", "need", "required", "optional",
        "maybe", "probably", "definitely", "please", "do", "does", "did", "all", "every", "each", "any", "some",
        "most", "few", "many", "but", "however", "although", "though", "despite", "while", "whereas", "rather",
    ]
)
assert CUTTABLE_PROTECTED <= PROTECTED
_NEGATIONS = frozenset(k for k in PROTECTED if k == "not" or k.endswith("n't") or k == "cannot")
# Words that make what they're said with conditional ("approve it if they agree"). Phrases,
# matched whole; the one-word ones are protected above, the rest are made of common words.
CONDITIONS: frozenset[tuple[str, ...]] = frozenset(tuple(normalize_key(w) for w in p.split()) for p in [
    "if", "unless", "whether", "provided", "providing", "assuming", "only if", "as long as", "so long as", "in case",
])
assert all(p[0] in PROTECTED for p in CONDITIONS if len(p) == 1) and not CUTTABLE_PROTECTED & {"if", "unless"}

# Verbal tics a ramble cut may drop when set off by punctuation or opening a sentence
# ("So, like, I was thinking"; "it was, you know, fine"). Phrases, matched whole.
DISCOURSE: frozenset[tuple[str, ...]] = frozenset(
    tuple(normalize_key(w) for w in p.split())
    for p in [
        "so", "well", "like", "you know", "i mean", "basically", "literally", "actually", "honestly",
        "okay", "ok", "alright", "yeah", "anyway", "anyways", "kind of", "sort of", "okay so", "yeah so", "and so",
    ]
)
assert not any(k in PROTECTED for p in DISCOURSE for k in p)

# Cues that the words before them are being corrected: "at 7, actually 3", "John, sorry, Mary".
CORRECTION_CUES: frozenset[tuple[str, ...]] = frozenset(
    tuple(normalize_key(w) for w in p.split())
    for p in ["actually", "sorry", "i mean", "scratch that", "let me rephrase", "correction"]
)
assert not any(k in PROTECTED for p in CORRECTION_CUES for k in p)

_PRONOUNS = frozenset(["i", "we", "you", "he", "she", "they", "it", "i'm", "we're", "you're", "they're", "it's",
                       "i'll", "we'll", "i'd", "we'd", "i've", "we've"])
_RESTART_PRONOUNS = frozenset(["it", "they", "he", "she", "this", "that", "these", "those"])
_BREAKS = ("...", "…", "—", "–", "-", ",", ";", ":")  # where a speaker breaks off or restarts
_TRAILED = ("...", "…", "—", "–", "-")  # where a half-sentence was left hanging
# Words too common to say what a half-sentence was about.
_SMALL_WORDS = frozenset([
    "i", "me", "my", "you", "your", "we", "us", "our", "he", "she", "it", "they", "them", "a", "an", "the", "to",
    "of", "in", "on", "at", "for", "by", "with", "from", "as", "that", "this", "is", "are", "was", "be", "am",
    "so", "and", "or", "what", "which", "who", "there", "here", "just", "get", "got", "want", "like", "um", "uh",
])
_MAX_CUT = 30  # words in one cut; anything longer is content, not a false start
_CONTENT_CUTS = ("self-correction", "taken back", "trailed off", "aside")
# ...of which these may come out of a longer cut, the rest put back: they open a sentence and
# their words come back after them. A correction or an aside carved out of a paraphrase can
# leave anything ("what I need you to set up is... well, actually" -> "what I need you to set").
_CARVED_CUTS = ("taken back", "trailed off")

# Tics that are filler wherever they are, not only set off by commas: "I guess so I am",
# "give me like a song list". Not where the word means something: "I think so.", "so that",
# "so much"; "I like it", "looks like a bug", "something like that".
_LOOSE_TICS = frozenset(["so", "like", "basically", "literally", "honestly"])
_SO_MEANS = frozenset(["that", "much", "many", "far", "long", "too", "as", "on", "forth"])
_SO_AFTER = frozenset(["guess", "and", "yeah", "okay", "ok", "well", "um", "uh"])  # "I guess so I am"
_LIKE_AFTER = frozenset(["i", "you", "we", "they", "he", "she", "i'd", "you'd", "we'd", "they'd", "would", "really",
                         "look", "looks", "looked", "seem", "seems", "seemed", "sound", "sounds", "sounded", "feel",
                         "feels", "felt", "something", "anything", "nothing", "just", "don't", "doesn't", "didn't",
                         "not", "is", "was", "be", "kind", "sort", "more", "less", "much"])
_LIKE_BEFORE = frozenset(["this", "that", "these", "those", "it", "them", "him", "her", "me", "us", "you", "to"])
# An aside never takes a commitment or a request with it: "you should", "I need you to".
_ASIDE_KEEPS = frozenset(["must", "should", "need", "required", "please", "will", "shall", "do", "does", "did"])
_ASIDE_MIN, _ASIDE_MAX = 3, 20
_ONE_AFTER = frozenset(["a", "the", "this", "that", "which", "another", "long", "big", "small", "good", "bad", "new",
                        "old", "other", "last", "first", "right", "wrong", "same"])  # "one" a pronoun, not a number
_ASIDE_SHARE = 0.5  # of the words said, at most, in asides

# Thinking aloud: the speaker talking about the ramble, not the point. Matched whole, written
# out ("I'm" -> "i am", "even" dropped), set off by punctuation or a "so"/"but"/"and" that
# joins it on; never mind the negation in it. The lead-ins only before something else is said
# ("Here we go." can be the whole message).
_ALOUD = frozenset(tuple(p.split()) for p in [
    "what am i saying", "what was i saying", "what was i going to say", "where was i", "where was i going with this",
    "i do not know what i am saying", "i do not understand what i am saying", "i lost my train of thought",
])
_ALOUD_LEAD = frozenset(tuple(p.split()) for p in [
    "buckle up", "bear with me", "here we go", "here we are", "let me think", "let me see", "how do i put this",
    "how do i say this", "this is going to be a long ramble", "this is going to be a ramble",
    "this is going to be a long one", "sorry for the ramble",
])
_ALOUD_JOIN = frozenset(["so", "but", "and"])
_ALOUD_NEXT = frozenset(["i", "we", "he", "she", "they", "i'm", "we're", "they're", "i'll", "we'll"])  # never an object

# Fillers a cleanup pass may delete. Must never overlap PROTECTED.
FILLERS: frozenset[str] = frozenset(
    normalize_key(w)
    for w in [
        "um", "umm", "uh", "uhh", "uhm", "hmm", "hm", "mhm", "erm", "er", "ah", "eh",
        "अं", "अंम", "हम्म", "हं", "आँ", "ऊँ", "अरे",
    ]
)
assert not (FILLERS & PROTECTED), "a filler can never be a protected word"

# A joining "and" the cleaned text dropped right before a new bullet line ("milk, eggs and
# bread" -> one item per line). Only these; "or" changes meaning and is never dropped.
LIST_JOINERS: frozenset[str] = frozenset(normalize_key(w) for w in ["and", "और", "आणि"])
assert not (LIST_JOINERS & PROTECTED)

# Line-start markers the cleaned text may use for a list item; all render as "•" except "-".
# "1." / "1)" number the items and stay as they are.
_BULLETS = {"-": "-", "•": "•", "*": "•", "–": "•", "·": "•"}
_NUMBERED = re.compile(r"\d{1,2}[.)]")
_NUMBERED_LINE = re.compile(r"(?m)^([ \t]*)\d{1,2}[.)](?=[ \t])")
_ITEM_LINE = re.compile(r"^\s*(?:[-•*–·]|\d{1,2}[.)])\s+(.*)$")

# With ramble cuts, what an item may lose at its start once it's on its own line: the words
# joining it to the one before ("and", "then", "oh and also"), the ordinal the list now
# carries ("first", "second"; in a numbered list it must be that item's number), and the
# subject a list of steps repeats ("then you go to settings" -> "Go to settings").
LIST_GLUE: frozenset[str] = frozenset(normalize_key(w) for w in [
    "and", "then", "also", "oh", "plus", "so", "next", "lastly", "finally", "you", "we", "और", "आणि",
])
_ORDINALS = {"first": 1, "firstly": 1, "second": 2, "secondly": 2, "third": 3, "thirdly": 3, "fourth": 4,
             "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}

# Small rewrites that keep every word's meaning: contractions and spoken forms written out
# ("dont" -> "don't", "gonna" -> "going to"). Both sides are compared written out.
_WRITTEN_OUT = {
    "don't": "do not", "dont": "do not", "doesn't": "does not", "doesnt": "does not", "didn't": "did not",
    "didnt": "did not", "can't": "can not", "cant": "can not", "cannot": "can not", "won't": "will not",
    "wont": "will not", "isn't": "is not", "isnt": "is not", "aren't": "are not", "arent": "are not",
    "wasn't": "was not", "wasnt": "was not", "weren't": "were not", "werent": "were not", "haven't": "have not",
    "havent": "have not", "hasn't": "has not", "hasnt": "has not", "hadn't": "had not", "hadnt": "had not",
    "wouldn't": "would not", "wouldnt": "would not", "shouldn't": "should not", "shouldnt": "should not",
    "couldn't": "could not", "couldnt": "could not", "mustn't": "must not",
    "i'm": "i am", "im": "i am", "you're": "you are", "youre": "you are", "we're": "we are", "they're": "they are",
    "theyre": "they are", "it's": "it is", "that's": "that is", "thats": "that is", "what's": "what is",
    "whats": "what is", "there's": "there is", "theres": "there is", "here's": "here is", "he's": "he is",
    "she's": "she is", "i've": "i have", "ive": "i have", "you've": "you have", "we've": "we have",
    "they've": "they have", "i'll": "i will", "you'll": "you will", "we'll": "we will", "they'll": "they will",
    "it'll": "it will", "that'll": "that will", "i'd": "i would", "you'd": "you would", "we'd": "we would",
    "they'd": "they would", "let's": "let us",
    "gonna": "going to", "wanna": "want to", "gotta": "got to", "kinda": "kind of", "sorta": "sort of",
    "lemme": "let me", "gimme": "give me", "dunno": "do not know", "outta": "out of", "lotta": "lot of",
    "cuz": "because", "coz": "because",
}
# Contractions missing their apostrophe: fine to fix, never to introduce.
_BARE = frozenset(k for k, v in _WRITTEN_OUT.items() if "'" not in k and k != "cannot"
                  and len(v.split()) == 2 and v.split()[1] in ("not", "am", "are", "is", "have"))
# Agreement: the same verb for another subject ("it get" -> "it gets", "if it dont" -> "if it
# doesn't"). Never a tense: "fix" -> "fixed" says it's done.
_AUX_FORMS = [frozenset(p) for p in (("is", "are", "am"), ("was", "were"), ("has", "have"), ("do", "does"))]
_SUBJECTS = frozenset(["i", "you", "we", "they", "it", "he", "she", "that", "this", "which", "who", "what", "there"])
_BE = frozenset(["am", "is", "are"])


class EditKind(StrEnum):
    DELETE = "delete"
    INSERT = "insert"
    REPLACE = "replace"


@dataclass(slots=True)
class Edit:
    kind: EditKind
    raw: str
    cleaned: str
    accepted: bool
    reason: str
    raw_span: tuple[int, int]  # token indices into the raw transcript


@dataclass(slots=True)
class GuardPolicy:
    allow_filler_deletion: bool = True
    allow_stutter_collapse: bool = True
    # Ramble cleanup: delete false starts, repeats, self-corrections and verbal tics (_cut_ok).
    allow_cuts: bool = False
    # ...and asides: whole clauses off the point (with allow_cuts; off when they'd take too much).
    allow_asides: bool = True
    # (heard_key, meant_key) pairs the vocab pass applied; keys are normalize_key'd.
    vocab_replacements: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    # Extra tokens the deployment wants protected (normalized on construction).
    extra_protected: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class GuardResult:
    text: str
    edits: list[Edit]

    @property
    def rejected(self) -> list[Edit]:
        return [e for e in self.edits if not e.accepted]


def _protected(keys: list[str], policy: GuardPolicy) -> bool:
    return any(k in PROTECTED or k in policy.extra_protected for k in keys)


def _keys(tokens: list[Token]) -> list[str]:
    return [t.key for t in tokens]


def guard(raw_text: str, cleaned_text: str, policy: GuardPolicy | None = None) -> GuardResult:
    """Return the cleaned text with every disallowed edit reverted to the raw words."""
    policy = policy or GuardPolicy()
    if not _lists_ok(cleaned_text):
        cleaned_text = unlist(cleaned_text)
    raw = tokenize(raw_text)
    cleaned = tokenize(_NUMBERED_LINE.sub(r"\1", cleaned_text))  # "1." is layout, not a word

    # Align on non-punctuation tokens only; punctuation-only tokens ride along with the
    # side that wins each region.
    raw_words = [t for t in raw if not t.is_punct]
    cln_words = [t for t in cleaned if not t.is_punct]
    layout = _layout(cleaned_text)
    sm = SequenceMatcher(a=_keys(raw_words), b=_keys(cln_words), autojunk=False)

    out: list[tuple[str, Token]] = []  # (layout in front of the token, token)
    edits: list[Edit] = []
    seam = False  # raw words were just put back mid-sentence
    broken_list = False
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        r = raw_words[i1:i2]
        c = cln_words[j1:j2]
        if op == "equal":
            pairs = list(zip(layout[j1:j2], c))  # cleaned surface: casing/punctuation/layout are free
            # ...except at a seam: "Hey, can you" with "Hey," deleted becomes "Can you"; putting
            # "Hey," back must undo the sentence-start capital the deletion caused.
            if seam and _sentence_start(cln_words, j1) and r[0].surface[:1].islower() and c[0].surface[:1].isupper():
                pairs[0] = (pairs[0][0], Token(c[0].surface[:1].lower() + c[0].surface[1:], c[0].key))
            out.extend(pairs)
            seam = False
            continue

        accepted = False
        reason = ""
        if op == "delete":
            # A deletion may be accepted in parts: the ramble cuts go, the rest is put back.
            for s, e, why in _deletion_plan(raw_words, i1, i2, layout, j1, policy):
                seg = raw_words[s:e]
                kept = why is None
                if kept:
                    # Words put back in front of an item land at the end of the one before it.
                    broken_list = broken_list or _starts_item(layout, j1)
                    why = "protected word deleted" if _protected(_keys(seg), policy) else "content word deleted"
                    _restore(out, seg, raw_words[s - 1] if s else None)
                    seam = not seg[-1].surface.endswith(_SENTENCE_END) or seg[-1].surface.endswith(_TRAILED)
                edits.append(Edit(EditKind.DELETE, detokenize(seg), "", not kept, why, (s, e)))
            continue
        if op == "insert":
            reason = "word inserted"
            kind = EditKind.INSERT
            broken_list = broken_list or _starts_item(layout, j1)  # the item's bullet goes with it
        else:  # replace
            kind = EditKind.REPLACE
            accepted, reason = _replace_ok(raw_words, i1, i2, c, policy)
            if not accepted and (split := _split_replace(raw_words, i1, i2, c, layout, j1, j2, policy)):
                # A rewrite next to a cut ("22 year old... what am I saying?" -> "22-year-old."):
                # each part passed on its own.
                edits.extend(split)
                out.extend(zip(layout[j1:j2], c))
                seam = False
                continue
        edits.append(
            Edit(
                kind=kind,
                raw=detokenize(r),
                cleaned=detokenize(c),
                accepted=accepted,
                reason=reason,
                raw_span=(i1, i2),
            )
        )
        seam = False
        if accepted:
            out.extend(zip(layout[j1:j2], c))
        else:
            # A reverted span keeps the line break the cleaned text put in front of it, and loses
            # its stutters all the same ("it's, it's kind of" swapped for "is kind of"), but not
            # a number's or a protected word's ("keep 0 0 files" is two numbers).
            back = list(range(i1, i2))
            if policy.allow_stutter_collapse:
                back = [n for n in back if not (n + 1 < i2 and _is_stutter(raw_words, n, n + 1)
                                                and not _never_cut(raw_words[n].key, policy))]
                if dropped := [n for n in range(i1, i2) if n not in back]:
                    edits.append(Edit(EditKind.DELETE, " ".join(raw_words[n].surface for n in dropped), "", True,
                                      "stutter", (dropped[0], dropped[-1] + 1)))
            words = [raw_words[n] for n in back]
            if words and c:  # ...and the punctuation and capital the cleaned text gave its stand-in
                last, cut_to = words[-1], c[-1].surface.rstrip(".?!।,;:…")
                if last.surface == last.surface.rstrip(".?!।,;:…—–-") and len(cut_to) < len(c[-1].surface):
                    words[-1] = Token(last.surface + c[-1].surface[len(cut_to):], last.key)
                if _sentence_start(cln_words, j1) and c[0].surface[:1].isupper() and words[0].surface[:1].islower():
                    words[0] = Token(words[0].surface[:1].upper() + words[0].surface[1:], words[0].key)
            # Mid-sentence once they're back, unless they took the cleaned text's full stop with them.
            seam = bool(words) and not words[-1].surface.endswith(_SENTENCE_END)
            _restore(out, words, raw_words[i1 - 1] if i1 else None, layout[j1] if j2 > j1 else "")

    aside = sum(e.raw_span[1] - e.raw_span[0] for e in edits if e.accepted and "aside" in e.reason)
    if aside > _ASIDE_SHARE * len(raw_words):  # cutting that much is summarising, not cleaning up
        return guard(raw_text, cleaned_text, replace(policy, allow_asides=False))
    if broken_list:  # half a list reads worse than none: the same text as sentences
        return guard(raw_text, unlist(cleaned_text), policy)
    return GuardResult(text=_reflow(out), edits=edits)


def _restore(out: list[tuple[str, Token]], words: list[Token], before: Token | None, pre: str = "") -> None:
    """Put raw words back. If the cleaned text ended a sentence right before them where the
    speaker didn't (`before` is the raw word there), that full stop goes: the sentence it
    ended continues with these words ("tomorrow. I think." -> "tomorrow I think."), and where
    the speaker ended one and the cleaned text didn't, it comes back. If the
    word before them was cut and they now open a sentence, they get its capital ("So what"
    with "So" cut -> "What")."""
    if not words:
        return
    first = words[0]
    if out and before is not None and out[-1][1].key == before.key:
        last = out[-1][1]
        if last.surface.endswith(_SENTENCE_END) and not before.surface.endswith(_SENTENCE_END):
            tail = before.surface[len(before.surface.rstrip(",;:—–-…")):]
            out[-1] = (out[-1][0], Token(last.surface.rstrip(".?!।") + tail, last.key))
        elif before.surface.endswith(_SENTENCE_END) and not last.surface.endswith(_SENTENCE_END):
            # ...and the other way round: they ended a sentence there, so it ends again.
            tail = before.surface[len(before.surface.rstrip(".?!।…")):]
            out[-1] = (out[-1][0], Token(last.surface.rstrip(",;:") + tail, last.key))
        elif before.surface.endswith(_BREAKS) and last.surface == last.surface.rstrip(".?!।,;:—–-…"):
            # ...and a comma the cleaned text dropped along with these words: "I want, what I want".
            tail = before.surface[len(before.surface.rstrip(",;:—–-…")):]
            out[-1] = (out[-1][0], Token(last.surface + tail, last.key))
    elif before is not None and (not out or out[-1][1].surface.endswith(_SENTENCE_END)) and first.surface[:1].islower():
        first = Token(first.surface[:1].upper() + first.surface[1:], first.key)
    out.append((pre, first))
    out.extend(("", t) for t in words[1:])


def _deletion_plan(words: list[Token], i1: int, i2: int, layout: list[str], j1: int,
                   policy: GuardPolicy) -> list[tuple[int, int, str | None]]:
    """Split a deleted span into (start, end, reason) parts; reason None = put back."""
    rk = _keys(words[i1:i2])
    if not _protected(rk, policy):
        if policy.allow_filler_deletion and all(k in FILLERS for k in rk):
            return [(i1, i2, "filler")]
        if policy.allow_stutter_collapse and _is_stutter(words, i1, i2) and not any(map(_is_number, rk)):
            return [(i1, i2, "stutter")]  # not a number's: "twenty twenty" is 2020
        if len(rk) == 1 and rk[0] in LIST_JOINERS and _starts_item(layout, j1):
            return [(i1, i2, "list joiner")]
    if policy.allow_cuts and _starts_item(layout, j1):
        t = i2  # one copy of a stutter goes with it: "oh and the the telemetry"
        while t > i1 and t < len(words) and words[t - 1].key == words[t].key:
            t -= 1
        if _list_glue(rk[:t - i1], layout[j1]):
            return [(i1, i2, "list joiner")]
    if not policy.allow_cuts:
        return [(i1, i2, None)]
    if why := _cut_ok(words, i1, i2, policy):
        return [(i1, i2, why)]
    return _partial_cuts(words, i1, i2, policy)


def _replace_ok(raw_words: list[Token], i1: int, i2: int, c: list[Token], policy: GuardPolicy) -> tuple[bool, str]:
    """Whether raw_words[i1:i2] may be written as `c`, and why (or why not)."""
    r = raw_words[i1:i2]
    rk, ck = _keys(r), _keys(c)
    surfaces = [t.surface for t in c]
    prev = raw_words[i1 - 1].key if i1 else ""
    # An apostrophe put in ("doesnt" -> "doesn't") is fine even on a negation; one taken
    # out isn't ("won't" -> "wont").
    if _same_after_fold(rk, ck) and (not (_protected(rk, policy) or _protected(ck, policy))
                                     or "".join(ck).count("'") > "".join(rk).count("'")):
        return True, "spelling variant"
    if _written_out_ok(rk, ck, prev):
        return True, "grammar"
    if _protected(rk, policy) or _protected(ck, policy):
        return False, "protected word changed"
    if _vocab_ok(rk, ck, policy, raw_words, i1):
        return True, "vocab"
    if _number_ok(rk, ck, surfaces) or _time_ok(rk, ck, surfaces) or _numbers_ok(rk, ck, surfaces):
        return True, "number"
    # A replace can hide a filler deletion plus an unrelated change; only accept if what
    # remains after removing fillers matches exactly (or is the same number in digits:
    # "uh, three" -> "3").
    r_nf = [k for k in rk if k not in FILLERS]
    if policy.allow_cuts:  # "like two" -> "2": the tic goes, the number turns into digits
        r_nf = [w.key for n, w in enumerate(r) if w.key not in FILLERS and not (
            w.key in _LOOSE_TICS and _loose_tic(raw_words, i1 + n))]
    if policy.allow_filler_deletion and r_nf == ck:
        return True, "filler"
    if policy.allow_filler_deletion and r_nf != rk and r_nf and (
            _number_ok(r_nf, ck, surfaces) or _time_ok(r_nf, ck, surfaces)):
        return True, "number"
    if policy.allow_filler_deletion and r_nf != rk and r_nf and _written_out_ok(r_nf, ck, prev):
        return True, "grammar"
    return False, "content word changed"


def _split_replace(raw_words: list[Token], i1: int, i2: int, c: list[Token], layout: list[str], j1: int, j2: int,
                   policy: GuardPolicy) -> list[Edit] | None:
    """A replace that is a rewrite plus a deletion right before or after it, each allowed on
    its own: the edits, or None."""
    for k in range(i1 + 1, i2):
        for rs, re_, ds, de, j in ((i1, k, k, i2, j2), (k, i2, i1, k, j1)):
            if re_ - rs > len(c) + 4:  # a rewrite is a few words for a few words
                continue
            ok, why = _replace_ok(raw_words, rs, re_, c, policy)
            if not ok:
                continue
            plan = _deletion_plan(raw_words, ds, de, layout, j, policy)
            if any(p[2] is None for p in plan):
                continue
            rep = Edit(EditKind.REPLACE, detokenize(raw_words[rs:re_]), detokenize(c), True, why, (rs, re_))
            cuts = [Edit(EditKind.DELETE, detokenize(raw_words[s:e]), "", True, w, (s, e)) for s, e, w in plan]
            return [rep, *cuts] if rs == i1 else [*cuts, rep]
    return None


def _numbers_ok(rk: list[str], ck: list[str], surfaces: list[str]) -> bool:
    """Spoken numbers in a row, each written as digits: "one, two, three" -> "1, 2, 3"."""
    return len(rk) == len(ck) > 1 and all(
        a == b or _number_ok([a], [b], [s]) for a, b, s in zip(rk, ck, surfaces)) and rk != ck


_SENTENCE_END = (".", "?", "!", "।")


def _sentence_start(words: list[Token], j: int) -> bool:
    return j == 0 or words[j - 1].surface.endswith(_SENTENCE_END)


def _layout(text: str) -> list[str]:
    """For each word token of `text` (same order as tokenize() minus punctuation), the
    layout in front of it: "", "\n" or "\n\n", plus "• " when it starts a list item.
    Only whitespace and bullet markers ever come from here."""
    out: list[str] = []
    breaks = 0
    for n, line in enumerate(text.strip().split("\n")):
        if n:
            breaks += 1
        bullet = ""
        first = True
        toks = line.split()
        for i, t in enumerate(toks):
            if i == 0 and len(toks) > 1 and _NUMBERED.fullmatch(t):  # "25." alone is a number, not a marker
                bullet = t + " "
                continue
            if normalize_key(t) == "":
                if i == 0 and t in _BULLETS:
                    bullet = _BULLETS[t] + " "
                continue
            if first:
                out.append("\n" * min(breaks, 2) + bullet)
                breaks, first = 0, False
            else:
                out.append("")
    return out


def _ends_sentence(w: Token) -> bool:
    """A full stop, question or exclamation; not "..." trailing off."""
    return w.surface.endswith(_SENTENCE_END) and not w.surface.endswith(_TRAILED)


def _starts_item(layout: list[str], j: int) -> bool:
    mark = layout[j].lstrip("\n")[:1] if j < len(layout) else ""
    return mark in ("•", "-") or mark.isdigit()


def _list_glue(keys: list[str], marker: str) -> bool:
    """`keys`, cut right before a list item, only joined it to the one before or numbered it."""
    num = marker.strip().rstrip(".)")
    n = int(num) if num.isdigit() else None
    if not 0 < len(keys) <= 4:
        return False
    for k in keys:
        if k in FILLERS or k in LIST_GLUE or k in _LOOSE_TICS or (k,) in DISCOURSE:
            continue
        # An ordinal or a spoken number the list now carries: anything goes in a bullet list,
        # only that item's own number in a numbered one ("third" before "2." is not glue).
        val = _ORDINALS.get(k, words_to_number([k]) if _is_number(k) and not k.isdigit() else None)
        if val is None or (n is not None and val != n):
            return False
    return True


def unlist(text: str) -> str:
    """The text with its list items run back into sentences: short items as one comma
    list after their intro line, longer ones as a sentence each."""
    lines, out = text.split("\n"), []
    i = 0
    while i < len(lines):
        if not _ITEM_LINE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        items = []
        while i < len(lines) and (m := _ITEM_LINE.match(lines[i])):
            items.append(m.group(1).strip())
            i += 1
        if all(len(t.split()) <= 6 and not t.rstrip(".").endswith(_SENTENCE_END) for t in items):
            run = ", ".join(t.rstrip(".,;") if n == 0 else _lower_first(t.rstrip(".,;")) for n, t in enumerate(items)) + "."
        else:
            run = " ".join(t[:1].upper() + t[1:] + ("" if t.endswith((*_SENTENCE_END, *_BREAKS)) else ".")
                           for t in items)
        if out and out[-1].strip():
            intro = out[-1].rstrip()
            if intro.endswith(":") and _dangling(intro[:-1]):  # "Thinking from:" was half a sentence
                intro, run = intro[:-1], _lower_first(run)
            out[-1] = intro + " " + run
        else:
            out.append(run)
    return "\n".join(out)


# A list intro ending in one of these is half a sentence ("Thinking from:", "I went to the:").
_DANGLING = frozenset([
    "a", "an", "the", "of", "to", "in", "on", "at", "for", "from", "with", "about", "by", "into", "onto", "like",
    "than", "between", "through", "over", "under", "and", "or", "but", "so", "because", "that", "which", "who",
    "my", "our", "your", "their", "his", "her", "its", "me", "us", "him", "them",
])
# Item openers that are lowercase mid-sentence once the intro runs on into them.
_LOWER_AFTER_INTRO = frozenset(["the", "a", "an", "how", "what", "why", "when", "where", "who", "which", "that",
                                "this", "these", "those", "my", "our", "your", "their", "his", "her", "its"])
_MIN_ITEMS = 2  # one item is not a list


def _lower_first(item: str) -> str:
    """An item run on mid-sentence: "The strike" -> "the strike", never a name."""
    first = item.split(maxsplit=1)[0] if item.strip() else ""
    return item[:1].lower() + item[1:] if normalize_key(first) in _LOWER_AFTER_INTRO else item


def _dangling(intro: str) -> bool:
    words = intro.split()
    last = normalize_key(words[-1]) if words else ""
    return last in _DANGLING or _is_number(last)


def _lists_ok(text: str) -> bool:
    """Every list in `text` has two or more items, and an intro (the line before it, when it
    ends in a colon) that isn't half a sentence or a lone number ("One:")."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if not _ITEM_LINE.match(lines[i]):
            i += 1
            continue
        start = i
        while i < len(lines) and _ITEM_LINE.match(lines[i]):
            i += 1
        if i - start < _MIN_ITEMS:
            return False
        intro = next((ln.strip() for ln in reversed(lines[:start]) if ln.strip()), "")
        if intro.endswith(":") and _dangling(intro[:-1]):
            return False
    return True


def _is_stutter(words: list[Token], i1: int, i2: int) -> bool:
    """True if the deleted span immediately repeats the tokens that follow or precede it."""
    n = i2 - i1
    if n == 0:
        return False
    deleted = [w.key for w in words[i1:i2]]
    after = [w.key for w in words[i2 : i2 + n]]
    before = [w.key for w in words[max(0, i1 - n) : i1]]
    # Never across a sentence end: "Do not deploy. Deploy it now." is two sentences, and
    # merging them would give "Do not deploy it now."
    return (deleted == after and not _ends_sentence(words[i2 - 1])) or (
        deleted == before and i1 > 0 and not _ends_sentence(words[i1 - 1]))


def _cut_ok(words: list[Token], i1: int, i2: int, policy: GuardPolicy) -> str | None:
    """Why deleting words[i1:i2] is a safe ramble cut, or None. The span must split into
    consecutive pieces that are each a filler, a set-off verbal tic, a repeat, a restart, a
    self-correction, a trailed-off half-sentence or an aside."""
    if not 0 < i2 - i1 <= _MAX_CUT:
        return None
    # The alignment may have dropped either copy of a repeated word ("start reading start
    # the" can lose "reading start" or "start reading"): try every equivalent span.
    lo, hi = i1, i2
    while lo > 0 and words[lo - 1].key == words[hi - 1].key:
        lo, hi = lo - 1, hi - 1
    while not (why := _covered(words, lo, hi, policy)):
        if hi >= len(words) or words[lo].key != words[hi].key:
            return None
        lo, hi = lo + 1, hi + 1
    return why


def _partial_cuts(words: list[Token], i1: int, i2: int, policy: GuardPolicy) -> list[tuple[int, int, str | None]]:
    """The cuts inside a span that can't go whole ("Hi, so I'm": "so" goes, "Hi," stays),
    keeping as few words as possible."""
    INF = 1 << 30
    cost = {i1: 0}
    back: dict[int, tuple[int, str | None]] = {}
    for e in range(i1 + 1, i2 + 1):
        best, how = cost[e - 1] + 1, (e - 1, None)  # keep words[e - 1]
        for s in range(max(i1, e - _MAX_CUT), e):
            # Other content cuts have to be exactly what the LLM cut (_CARVED_CUTS); carved out, a
            # half-sentence has to come back whole: "send the invoice... no, I don't want to send it
            # yet" can't lose "the invoice".
            if cost.get(s, INF) < best and (why := _piece(words, s, e, policy)) is not None and (
                    why not in _CONTENT_CUTS or why in _CARVED_CUTS) and (why != "trailed off" or _said_again(words, s, e)):
                best, how = cost[s], (s, why)
        cost[e], back[e] = best, how
    parts: list[tuple[int, int, str | None]] = []
    e = i2
    while e > i1:
        s, why = back[e]
        if why is None and parts and parts[-1][2] is None and parts[-1][0] == e:
            parts[-1] = (s, parts[-1][1], None)  # one span of kept words
        else:
            parts.append((s, e, why))
        e = s
    return parts[::-1]


def _covered(words: list[Token], i1: int, i2: int, policy: GuardPolicy) -> str | None:
    # best[e] = the reason the span up to e is covered (DP over piece boundaries). A cover
    # without an aside beats one with: "okay so I was gonna," is a tic and a false start.
    best: dict[int, str] = {i1: ""}
    for s in range(i1, i2):
        if s not in best:
            continue
        for e in range(s + 1, i2 + 1):
            if (e not in best or "aside" in best[e]) and (why := _piece(words, s, e, policy)):
                how = why if not best[s] or why in best[s] else f"{best[s]} + {why}"
                if e not in best or "aside" not in how:
                    best[e] = how
    return best.get(i2) or None


def _piece(words: list[Token], s: int, e: int, policy: GuardPolicy) -> str | None:
    keys = [w.key for w in words[s:e]]
    if all(k in FILLERS for k in keys):
        return "filler"
    if _aloud(words, s, e):
        return "thinking aloud"
    if _retraction(words, s, e, policy):
        return "taken back"
    hard = any(_never_cut(k, policy) for k in keys)
    soft = any(k in PROTECTED or k in policy.extra_protected for k in keys)
    broke_off = words[e - 1].surface.endswith(_BREAKS)
    starts = _starts_sentence(words, s)
    # A verbal tic opening a sentence ("So, like, I was thinking"), set off by commas on both
    # sides ("it was, you know, fine"), or "I mean" after one ("the socket, I mean the websocket").
    after_break = s > 0 and words[s - 1].surface.endswith(_BREAKS)
    if tuple(keys) in DISCOURSE and not _ends_sentence(words[e - 1]) and (  # "Okay." on its own is a reply
            starts or (after_break and (broke_off or keys == ["i", "mean"]))):
        return "verbal tic"
    if len(keys) == 1 and keys[0] in _LOOSE_TICS and _loose_tic(words, s):
        return "verbal tic"
    n = e - s
    # Last resort, so a repeat or a tic keeps its own name: a whole clause off the point
    # ("a long one" is no number).
    aside = None
    if policy.allow_asides and not any(_never_cut(w.key, policy) and not (w.key == "one" and n > 0 and words[n - 1].key in _ONE_AFTER)
                                       for n, w in enumerate(words[s:e], s)) and _aside(words, s, e):
        aside = "aside"
    # Said again right after: the next words start over the same way and every word of the
    # piece reappears ("I want a plan that would... I live here so I want a plan that would").
    # A repeat or restart ends mid-sentence, or is a whole sentence said again; never the end
    # of a sentence whose next one happens to start the same ("Do not deploy. Deploy it now.").
    if _ends_sentence(words[e - 1]) and not starts:
        return aside
    after = {w.key for w in words[e : e + n + 6]}
    if e < len(words) and words[e].key == keys[0] and all(k in after for k in keys):
        return "repeat"
    # ...or starts over with a pronoun for what it was about: "the bubble should, it should be"
    # -> "the bubble should be". Only for a thing named, never one pronoun for another ("we
    # won't, they won't").
    m = n - 1
    if (2 <= n <= 4 and keys[0] in _RESTART_PRONOUNS and s > m and words[s - 1].surface.endswith(_BREAKS)
            and [w.key for w in words[s - m:s]] == keys[1:] and words[s - m - 1].key not in _PRONOUNS
            and words[s - m - 1].key not in _RESTART_PRONOUNS and not _never_cut(words[s - m - 1].key, policy)):
        return "repeat"
    # A self-correction: "at 7, actually 3" (the corrected words and the cue go; numbers too,
    # that's the point, but never a protected word). The cue follows a break and the right
    # words follow it: not "I'm sorry, I can't", not "200, actually." at the end.
    for cue in CORRECTION_CUES:
        m = len(cue)
        if (n > m and tuple(keys[-m:]) == cue and n - m <= 3 and not soft and e < len(words)
                and words[e - m - 1].surface.endswith(_BREAKS) and not _ends_sentence(words[e - 1])
                and keys[0] not in _PRONOUNS):
            return "self-correction"
    if hard:
        return aside
    # A restart: "start reading start the meeting", "I was gonna, I want to", "we we're". Never
    # from a tic said again: "you are like kids something like the end" restarts nothing.
    if (n <= 3 and not soft and e < len(words) and words[e].key == keys[0]
            and (keys[0] not in _PRONOUNS or broke_off) and (n == 1 or keys[0] not in _LOOSE_TICS)):
        return "false start"
    if n == 1 and e < len(words) and words[e].key.startswith(keys[0] + "'"):
        return "false start"
    # A half-sentence left hanging and then said again another way: "so what I need you to set
    # up is... no, I don't want you to set up". Most of what it was about has to come back
    # (Whisper also writes "..." for a mere pause), and never with a number.
    if words[e - 1].surface.endswith(_TRAILED) and starts and not any(_is_number(k) for k in keys) \
            and _said_again(words, s, e, 0.5):
        return "trailed off"
    return aside


def _said_again(words: list[Token], s: int, e: int, share: float = 1.0) -> bool:
    """At least `share` of what words[s:e] was about comes back in the words after it."""
    gist = {w.key for w in words[s:e] if w.key not in _SMALL_WORDS}
    later = {w.key for w in words[e : e + (e - s) + 12]}
    return len(gist & later) >= share * len(gist)


def _loose_tic(words: list[Token], i: int) -> bool:
    """words[i] ("so", "like", ...) is filler here, wherever it is in the sentence."""
    w = words[i]
    if _ends_sentence(w):  # "I think so."
        return False
    prev = words[i - 1].key if i else ""
    nxt = words[i + 1].key if i + 1 < len(words) else ""
    if w.key == "so":
        # Mid-sentence "so" is "therefore" ("I live in Millbrook so I want", "a long ramble so buckle
        # up"): cutting it runs two sentences together. Only after a comma or "I guess so I".
        after_break = i > 0 and words[i - 1].surface.endswith(_BREAKS)
        return bool(nxt) and nxt not in _SO_MEANS and (after_break or prev in _SO_AFTER)
    if w.key == "like":
        return bool(nxt) and prev not in _LIKE_AFTER and nxt not in _LIKE_BEFORE
    return True


def _aside(words: list[Token], s: int, e: int) -> bool:
    """words[s:e] is a whole clause or sentence the LLM may drop as off the point: nothing in it
    that the point could hang on. Negations, time words, conditions and numbers are checked by
    the caller; here: commitments, requests and names said nowhere else ("I live in
    Ashgrove"). Whisper's own punctuation (or the edge of the text) bounds it on both
    sides: where Whisper ran clauses on, "I told him I would come" -> "I would come" looks
    just like a preamble going, so nothing is cut."""
    keys = [w.key for w in words[s:e]]
    if not _ASIDE_MIN <= len(keys) <= _ASIDE_MAX:
        return False
    ends = (*_BREAKS, *_SENTENCE_END)
    if not ((s == 0 or words[s - 1].surface.endswith(ends)) and (e == len(words) or words[e - 1].surface.endswith(ends))):
        return False
    # Whole sentences, or a clause inside one: never the opening words of the next sentence.
    if e < len(words) and not _ends_sentence(words[e - 1]) and any(_ends_sentence(w) for w in words[s:e - 1]):
        return False
    if any(k in _ASIDE_KEEPS for k in keys):
        return False
    if set(pairwise(keys)) & {("you", "to"), ("can", "you"), ("could", "you"), ("would", "you"), ("want", "you")} \
            or "let's" in keys:
        return False
    rest = {w.key for w in words[:s]} | {w.key for w in words[e:]}
    for n, w in enumerate(words[s:e], s):
        named = w.surface[:1].isupper() and n > 0 and not _ends_sentence(words[n - 1]) \
            and not words[n - 1].surface.endswith(_TRAILED) and not w.key.startswith("i'") and w.key != "i"
        if named and w.key not in rest:
            return False
    return True


def _aloud(words: list[Token], s: int, e: int) -> bool:
    """words[s:e] is thinking aloud: "what am I saying?", "buckle up but", "here we are I guess so"."""
    ks = [x for w in words[s:e] for x in _WRITTEN_OUT.get(w.key, w.key).split() if x not in ("even", "really")]
    joined = len(ks) > 1 and ks[-1] in _ALOUD_JOIN and not words[e - 1].surface.endswith(_BREAKS + _SENTENCE_END)
    if joined:
        ks.pop()
    if ks[-2:] == ["i", "guess"]:
        ks = ks[:-2]
    phrase = tuple(ks)
    if phrase not in _ALOUD and (phrase not in _ALOUD_LEAD or e >= len(words)):
        return False
    ends = (*_BREAKS, *_SENTENCE_END)
    left = s == 0 or words[s - 1].surface.endswith(ends) or words[s - 1].key in _ALOUD_JOIN
    return left and (joined or e == len(words) or words[e - 1].surface.endswith(ends) or words[e].key in _ALOUD_NEXT)


def _retraction(words: list[Token], s: int, e: int, policy: GuardPolicy) -> bool:
    """words[s:e] is a half-sentence taken back and said again another way: "what I need you
    to set up is... well, actually no(, I don't want you to set up.) I want you to do some
    research". The "no" and the negation only take back what's cut with them: the negation
    says nothing the half-sentence didn't, most of that comes back after it, and something
    follows."""
    if e >= len(words) or not _starts_sentence(words, s):
        return False
    p = next((n for n in range(s, e) if words[n].surface.endswith(_TRAILED)), None)
    if p is None or any(_never_cut(w.key, policy) for w in words[s:p + 1]):
        return False
    q = p + 1
    while q < e and (words[q].key in FILLERS or (words[q].key,) in DISCOURSE or (words[q].key,) in CORRECTION_CUES):
        q += 1
    if q >= e or words[q].key != "no" or not words[q].surface.endswith(_BREAKS + _SENTENCE_END):
        return False
    half = {w.key for w in words[s:p + 1]}
    said = words[q + 1:e]
    # Kept after it, "it" would lose what it stood for: "send the invoice... no, I don't want to send it yet".
    rest = words[e:e + 12]
    if not said and any(w.key in ("it", "them", "that", "this", "one") and w.key not in half for w in rest):
        return False
    if said:
        hard = [w.key for w in said if _never_cut(w.key, policy)]
        if len(said) > 12 or not _ends_sentence(words[e - 1]) or len(hard) != 1 or hard[0] not in _NEGATIONS:
            return False
        if any(w.key not in half and w.key not in _SMALL_WORDS and w.key not in _NEGATIONS and w.key not in ("want", "need")
               for w in said):
            return False
    # Most of it comes back ("to John... no, to Mary"); all of it where what comes back is a
    # negation, which would otherwise say more than was taken back ("deploy it to staging...
    # no, never deploy it" is not "never deploy it").
    gist = {k for k in half if k not in _SMALL_WORDS and k not in ("want", "need")}
    later = {w.key for w in words[p + 1:e + len(half) + 12]}
    negated = bool(said) or any(w.key in _NEGATIONS or w.key in ("never", "no") for w in rest)
    return bool(gist) and len(gist & later) >= (1.0 if negated else 0.5) * len(gist)


def _starts_sentence(words: list[Token], s: int) -> bool:
    """Words[s] starts a sentence, or a restart after trailing off, possibly behind other
    tics: "So basically the", "is... Well, actually"."""
    ends = (*_SENTENCE_END, *_TRAILED)
    j = s - 1
    while j >= 0 and not words[j].surface.endswith(ends) and (
            words[j].key in FILLERS or (words[j].key,) in DISCOURSE):
        j -= 1
    return j < 0 or words[j].surface.endswith(ends)


def _never_cut(k: str, policy: GuardPolicy) -> bool:
    return (k in PROTECTED and k not in CUTTABLE_PROTECTED) or k in policy.extra_protected or _is_number(k)


def _is_number(k: str) -> bool:
    return any(ch.isdigit() for ch in k) or (k in _UNITS and k not in ("a", "oh")) or k in _TENS or k in _SCALES \
        or _compound(k) is not None


def _vocab_ok(rk: list[str], ck: list[str], policy: GuardPolicy, raw_words: list[Token] | None = None,
              i1: int = 0) -> bool:
    """A vocab replacement, whole, or the changed part of one ("1970" -> "9070" inside the rule
    "1970 XT" -> "9070 XT"), with the rest of the rule's words around it in what was said."""
    if not policy.vocab_replacements:
        return False
    if (" ".join(rk), " ".join(ck)) in policy.vocab_replacements:
        return True
    if raw_words is None or len(rk) != len(ck):
        return False
    keys = [w.key for w in raw_words]
    for heard, meant in policy.vocab_replacements:
        h, m = heard.split(), meant.split()
        if len(h) != len(m) or len(h) <= len(rk):
            continue
        for o in range(len(h) - len(rk) + 1):
            if (h[o:o + len(rk)] == rk and m[o:o + len(ck)] == ck
                    and h[:o] == m[:o] and h[o + len(rk):] == m[o + len(rk):]
                    and keys[i1 - o:i1 - o + len(h)] == h and i1 - o >= 0):
                return True
    return False


_UNITS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                                       "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"])}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_UNITS.update({"oh": 0, "a": 1})


def words_to_number(keys: list[str]) -> int | None:
    """'twenty five' -> 25, 'two thousand and six' -> 2006. None if not a number phrase.
    Words combine only as one number is said, never summed: a unit after a tens word ("twenty
    five"), anything after "hundred" or a scale ("two hundred five"). "twenty twenty" is not 40,
    "one one" not 2, "zero zero seven" not 7; a zero is a number only on its own, and digits
    with a leading zero ("007") are never a value."""
    total, current, seen = 0, 0, False
    state = "start"  # start | tens (a unit may follow) | unit (only a scale) | open (after a scale)
    digits = False  # the number so far ends in digits: only a scale may follow ("2 thousand")
    last_scale = 0
    words = [k for k in keys if k != "-"]
    several = sum(k != "and" for k in words) > 1
    for n, k in enumerate(words):
        if k == "and":  # "two hundred and five", never "twenty and five" (two numbers)
            if not n or words[n - 1] not in _SCALES:
                return None
            continue
        if digits and k not in _SCALES:  # "1 23", "0 23", "5 twenty": two numbers, never one
            return None
        if k in _UNITS:
            v = _UNITS[k]
            if v == 0 and several:  # "zero zero seven", "oh five": digits read out, not a value
                return None
            if state == "unit" or (state == "tens" and not 1 <= v <= 9):
                return None
            current += v
            state = "unit"
        elif k in _TENS or _compound(k) is not None:  # "twenty-five" (keys drop the hyphen: "twentyfive")
            if state not in ("start", "open") or current % 100:
                return None
            current += _TENS.get(k) or _compound(k)
            state = "tens" if k in _TENS else "unit"
        elif k == "hundred":
            if current >= 100 or (state == "open" and not current):  # nor "two thousand hundred"
                return None
            current = max(current, 1) * 100  # "hundred" alone is 100; a said 0 never gets here
            state, digits = "open", False
        elif k in _SCALES:
            if last_scale and _SCALES[k] >= last_scale:  # "two thousand three thousand"
                return None
            if state == "open" and not current:  # "two million thousand"
                return None
            total += max(current, 1) * _SCALES[k]
            current, digits, last_scale, state = 0, False, _SCALES[k], "open"
        elif k.isdigit():
            if current or state not in ("start", "open") or (len(k) > 1 and k[0] == "0"):  # nor "twenty 5" 205, "007" 7
                return None
            if int(k) == 0 and several:  # "0 million" is never a million: a zero only on its own
                return None
            current, digits, state = int(k), True, "unit"
        else:
            return None
        seen = True
    return total + current if seen else None


def _compound(k: str) -> int | None:
    for tens, v in _TENS.items():
        rest = k[len(tens):]
        if k.startswith(tens) and rest and _UNITS.get(rest, 0) in range(1, 10) and rest not in ("oh", "a"):
            return v + _UNITS[rest]
    return None


# Not "pounds": "weighs five pounds" is no £5.
_CURRENCY = {"dollar": "$", "dollars": "$", "rupee": "₹", "rupees": "₹", "euro": "€", "euros": "€", "bucks": "$"}


def _number_ok(rk: list[str], ck: list[str], surfaces: list[str]) -> bool:
    """Spoken number -> digits: 'twenty five' -> '25', 'five percent' -> '5%',
    'three hundred dollars' -> '$300', 'two thousand rupees' -> '₹2000'. The key keeps the
    sign, decimal point, percent and currency symbol (text.number_key), so all of those
    have to be what was said: "fifteen" is never "1.5", "five" never "-5" or "5%"."""
    if len(ck) != 1:
        return False
    unit_words = [k for k in rk if k in _CURRENCY or k == "percent"]
    n = words_to_number([k for k in rk if k not in _CURRENCY and k != "percent"])
    if n is None or len(unit_words) > 1:
        return False
    unit = unit_words[0] if unit_words else None
    digits = ck[0]
    if unit == "percent":
        if not digits.endswith("%"):
            return False
        digits = digits[:-1]
    elif unit is not None:
        sym = _CURRENCY[unit]
        if digits.startswith(sym):
            digits = digits[len(sym):]
        elif unit in ("rupee", "rupees") and digits.startswith("rs"):
            digits = digits[2:]
        else:
            return False
    return digits == str(n)  # "0" is never "00", "five" never "05"


def _written_out(keys: list[str]) -> list[str]:
    return [w for k in keys for w in _WRITTEN_OUT.get(k, k).split()]


def _written_out_ok(rk: list[str], ck: list[str], prev: str) -> bool:
    """The same words once contractions and spoken forms are written out, give or take
    agreement with the subject: "dont wanna" -> "don't want to", "we gonna" -> "we're going
    to", "it get" -> "it gets", "if it dont" -> "if it doesn't". Every "not" stays a "not"."""
    if any(k in _BARE and k not in rk for k in ck):
        return False
    a, b = _written_out(rk), _written_out(ck)
    i = j = 0
    while i < len(a) and j < len(b):
        before = a[i - 1] if i else prev
        if a[i] == b[j] or _agrees(a[i], b[j], before):
            i, j = i + 1, j + 1
        elif b[j] in _BE and a[i:i + 2] == ["going", "to"] and b[j + 1:j + 3] == ["going", "to"] and before in _SUBJECTS:
            j += 1  # "we gonna" said without the "are"
        else:
            return False
    return i == len(a) and j == len(b)


def _agrees(x: str, y: str, before: str) -> bool:
    if any(x in f and y in f for f in _AUX_FORMS):
        return True
    if before not in _SUBJECTS or x in PROTECTED or y in PROTECTED:
        return False
    short, long_ = sorted((x, y), key=len)
    return len(short) >= 2 and long_ in (short + "s", short + "es", short[:-1] + "ies")


_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def _time_ok(rk: list[str], ck: list[str], surfaces: list[str]) -> bool:
    """A spoken time in clock form: "5 30" / "five thirty" -> "5:30", "nine oh five pm" -> "9:05 pm"."""
    if len(rk) >= 2 and len(ck) == 2 and rk[-1] == ck[-1] and rk[-1] in ("am", "pm"):
        rk, ck, surfaces = rk[:-1], ck[:-1], surfaces[:-1]
    if len(ck) != 1 or len(rk) < 2 or not (m := _TIME.fullmatch(surfaces[0].rstrip(".,;:!?"))):
        return False
    h = words_to_number(rk[:1])
    m_keys = rk[1:]
    if len(m_keys) == 2 and m_keys[0] in ("oh", "zero") and 1 <= _UNITS.get(m_keys[1], 0) <= 9:
        m_keys = m_keys[1:]  # "nine oh five" -> 9:05
    mins = int(m_keys[0]) if len(m_keys) == 1 and re.fullmatch(r"\d\d", m_keys[0]) else words_to_number(m_keys)
    return h == int(m.group(1)) and mins == int(m.group(2)) and h <= 24 and mins < 60


def _same_after_fold(rk: list[str], ck: list[str]) -> bool:
    """Accept differences that are only hyphenation or apostrophe placement. Keys only keep a
    "-" in a number (a sign or a range: text.number_key), and that one is never free."""
    fold = lambda ks: "".join(ks).replace("'", "")  # noqa: E731
    return fold(rk) == fold(ck) and _digit_runs(rk) == _digit_runs(ck)


def _digit_runs(keys: list[str]) -> list[str]:
    """The digit runs in `keys`, token by token: "1 23" is [1, 23] and "12 3" is [12, 3], so
    joining or splitting numbers across tokens is never a spelling variant."""
    return [d for k in keys for d in re.findall(r"\d+", k)]


def _reflow(tokens: list[tuple[str, Token]]) -> str:
    """Join tokens with their layout, fixing the double-punctuation seams that reverting a
    span can leave."""
    parts: list[str] = []
    for n, (pre, t) in enumerate(tokens):
        if n == 0:
            parts.append(pre.lstrip("\n"))
        else:
            parts.append(pre if pre.startswith("\n") else " " + pre)
        parts.append(t.surface)
    text = "".join(parts)
    text = re.sub(r" ([,?!।])", r"\1", text)
    text = re.sub(r" \.(?!\d)", ".", text)  # not the point of ".5"
    text = text.replace(",,", ",")
    text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)  # a doubled full stop, not an ellipsis
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()
