"""Tokenization and normalization shared by the guard, cleanup and vocab matcher.

Everything here must be script-aware: Devanagari words carry combining marks (matras,
nukta, chandrabindu) that naive "strip non-letters" code destroys. We only ever strip
punctuation and symbols, never marks.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Devanagari danda / double danda count as sentence punctuation.
_PUNCT_CLASSES = {"Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Sm", "Sc", "Sk", "So"}
_APOSTROPHES = "'’ʼ"

# Unicode excludes the precomposed nukta letters (U+0958..U+095F) from composition, so
# after NFC a nukta is always a separate U+093C mark. Speakers write these either way,
# so we drop the mark for comparison and fold chandrabindu to anusvara.
_NUKTA = "\u093c"
_CHANDRABINDU = "\u0901"
_ANUSVARA = "\u0902"


@dataclass(frozen=True, slots=True)
class Token:
    """A whitespace-delimited surface token plus its comparison key."""

    surface: str
    key: str  # normalized form used for alignment; "" if the token is punctuation only

    @property
    def is_punct(self) -> bool:
        return self.key == ""


def strip_punct(s: str) -> str:
    """Remove punctuation/symbol characters but keep letters, digits and combining marks."""
    out = []
    for ch in s:
        if ch in _APOSTROPHES:
            out.append("'")
            continue
        cat = unicodedata.category(ch)
        if cat in _PUNCT_CLASSES:
            continue
        out.append(ch)
    return "".join(out)


def normalize_key(s: str) -> str:
    """Comparison key: NFC, casefold, punctuation stripped, Devanagari nukta folded.

    Leading/trailing apostrophes are dropped so "'cause" and "cause" collide, but
    internal ones stay ("don't" != "dont" would be a real difference in ASR output).
    A token with a digit in it keeps what its value hangs on (number_key): "1.5" is not
    "15", "-5" is not "5", "$5" is not "5".
    """
    s = unicodedata.normalize("NFC", s)
    s = number_key(s) if any(ch.isdigit() for ch in s) else strip_punct(s)
    s = s.casefold().replace(_NUKTA, "").replace(_CHANDRABINDU, _ANUSVARA)
    return s.strip("'")


_RANGE_DASHES = "-‐‑–—"
# What a sign in front of a number keys as: every dash and minus is "-" ("–5" is "-5"), and the
# approximate and comparison signs are themselves ("~5", "<5" and "±5" are not "5").
SIGNS = {**dict.fromkeys(_RANGE_DASHES + "−－﹣", "-"), "+": "+", "＋": "+",
         **{c: c for c in "±~<>≈≤≥"}}
# Digit grouping that leaves the value alone: 1,500 / 1,500,000 / 1,50,000 (Indian).
_GROUPED = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3})(?![\d,])")


def _number_at(s: str, i: int) -> bool:
    """A number starts at s[i], perhaps behind signs, unit marks and a decimal point: "5",
    "$5", "-$5", "$+5", "-%5", ".5", "$.5"."""
    while i < len(s) and (s[i] in SIGNS or s[i] in _UNIT_SIGNS or unicodedata.category(s[i]) == "Sc"):
        i += 1
    if i < len(s) and s[i] == ".":
        i += 1
    return i < len(s) and s[i].isdigit()


def number_key(s: str) -> str:
    """strip_punct for a token with digits, keeping everything its value hangs on: a decimal
    point (".5" is "0.5", never "5"), a clock or ratio colon, a slash, a range dash between
    digits, leading signs (SIGNS: "–5" is "-5", "~5" and "<5" keep theirs; also around a
    currency symbol, keyed signs first: "$-5" and "-$5" are both "-$5"; every one of a run: "≥-5"
    is not "-5", "-%5" not "5"), an exponent's minus ("1e-3"), a percent, per mille or degree sign
    and currency symbols. Thousands separators go
    ("1,500" = "1500"), any other comma between digits stays ("1,5" is not "15"), and so does
    punctuation around the number ("5." at the end of a sentence is "5")."""
    out = []
    n = len(s)
    for i, ch in enumerate(s):
        if ch in _APOSTROPHES:
            out.append("'")
            continue
        cat = unicodedata.category(ch)
        if cat not in _PUNCT_CLASSES:
            out.append(ch)
            continue
        prev_d = i > 0 and s[i - 1].isdigit()
        next_d = i + 1 < n and s[i + 1].isdigit()
        lead = not any(c.isalnum() for c in s[:i])  # nothing but symbols before it
        if ch in ".,:/" and prev_d and next_d:
            out.append(ch)
        elif ch == "." and next_d and lead:
            out.append("0.")
        elif ch in _RANGE_DASHES and prev_d and next_d:
            out.append("-")
        elif ch in SIGNS and lead and _number_at(s, i + 1):
            out.append(SIGNS[ch])
        elif ch in _UNIT_SIGNS and lead and _number_at(s, i + 1):
            out.append(ch)  # "-%5" keeps its "%": "- % 5" is never "5"
        elif SIGNS.get(ch) == "-" and next_d and i > 1 and s[i - 1] in "eE" and s[i - 2].isdigit():
            out.append("-")  # an exponent's minus: "1e-3" is not "1e3" ("1e+3" is)
        elif (ch in _UNIT_SIGNS and prev_d) or cat == "Sc":
            out.append(ch)
    key = "".join(out)
    head = 0
    while head < len(key) and (key[head] in _SIGN_KEYS or key[head] in _UNIT_SIGNS or _is_currency(key[head])):
        head += 1
    # Every mark kept: signs in the order said, then currency, then percent/degree ("$-5" is "-$5")
    key = "".join(sorted(key[:head], key=lambda c: 0 if c in _SIGN_KEYS else 2 if c in _UNIT_SIGNS else 1)) + key[head:]
    return _GROUPED.sub(lambda m: m.group().replace(",", ""), key)


_SIGN_KEYS = "".join(sorted(set(SIGNS.values())))
_UNIT_SIGNS = "%‰°"
_TRAIL = ".,;:!?"  # sentence punctuation a unit sign may carry: "5 %."
_BULLETS = ("-", "–")  # the dashes that open a list item; any other sign at line start is a sign


def _is_currency(ch: str) -> bool:
    return unicodedata.category(ch) == "Sc"


def _marks(w: str) -> bool:
    """A word that is only signs and unit marks: "-", "$", "$-", "%", "-$"."""
    return bool(w) and all(c in SIGNS or c in _UNIT_SIGNS or _is_currency(c) for c in w)


def _ends_number(t: str) -> bool:
    """`t` ends in a number, perhaps with unit marks after it: "5", "-5 %", "5€"."""
    t = t.rstrip()
    while t and (t[-1] in _UNIT_SIGNS or _is_currency(t[-1]) or t[-1].isspace()):
        t = t[:-1]
    return t[-1:].isdigit()


def join_marks(words: list[str], ranges: bool = False) -> list[str]:
    """Group one line's words so a number's marks written apart from it are part of it. A run
    of sign marks (SIGNS) and currency, percent, per mille or degree signs, in any order and
    spaced any way, right before a number joins it ("- $ 5", "$- 5", "€ - 5", "+ $5" -> one
    group each); percent, per mille, degree and currency signs right after one join it ("5 %",
    "- 5 %", "5 €"; but "3 $ 5" is 3 and "$ 5"). A lone "-" or "–" opening the line is a bullet
    (never a minus "−", "﹣", "－" or a "+"); with `ranges`, a lone dash between two numbers
    ("3 - 5", "3 - $5") is a range, not a sign.
    Groups are the words joined by a space."""
    out: list[str] = []
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and _marks(words[j].rstrip(_TRAIL)):
            j += 1
            if words[j - 1].rstrip(_TRAIL) != words[j - 1]:
                break  # "%." ends the run
        if j == i:
            out.append(words[i])
            i += 1
            continue
        run = words[i:j]
        follows = j < len(words) and _number_at(words[j], 0) and run[-1].rstrip(_TRAIL) == run[-1]
        k = 0
        if out and _ends_number(out[-1]):  # a unit after its number; a currency only if no number follows
            while k < len(run) and all(c in _UNIT_SIGNS or (_is_currency(c) and not follows)
                                       for c in run[k].rstrip(_TRAIL)):
                k += 1
            if k:
                out[-1] = " ".join([out[-1], *run[:k]])
        rest = run[k:]
        if not follows:
            out += rest
            i = j
            continue
        bullet = i == 0 and bool(rest) and rest[0] in _BULLETS
        dash = ranges and bool(out) and bool(rest) and len(rest[0]) == 1 and SIGNS.get(rest[0]) == "-" \
            and _ends_number(out[-1])  # "3 - 5"
        keep = int(bullet or dash)
        out += rest[:keep]
        out.append(" ".join([*rest[keep:], words[j]]))
        i = j + 1
    return out


def tokenize(text: str) -> list[Token]:
    """Split on whitespace, keeping the surface form and a normalized key per token. A sign or
    unit written apart from its number is part of it (join_marks): "- 5" is one token keyed
    "-5", "5 %" is "5%", "$ 5" is "$5", "$- 5" and "- $ 5" are "-$5". Not a dash opening a line
    (a bullet), nor one between words."""
    out: list[Token] = []
    for line in text.strip().split("\n"):
        out += [Token(surface=t, key=normalize_key(t.replace(" ", ""))) for t in join_marks(line.split())]
    return out


def detokenize(tokens: list[Token]) -> str:
    return " ".join(t.surface for t in tokens)


def script_of(word: str) -> str:
    """'deva', 'latin', 'digit', 'mixed' or 'other' for a single token."""
    kinds = set()
    for ch in word:
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:
            kinds.add("deva")
        elif ch.isascii() and ch.isalpha():
            kinds.add("latin")
        elif ch.isdigit():
            kinds.add("digit")
        elif unicodedata.category(ch)[0] in "LM":
            kinds.add("other")
    if not kinds:
        return "other"
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed"
