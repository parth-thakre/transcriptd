"""Learning from what the user does to a dictation after it's inserted.

The clients read the text field back when the user is done with it (the phone: when the message
is sent or they move on; the desktop agent: for a minute after a paste) and post the whole field
to /v1/dictations/{id}/edited. From that:

- `locate` finds the user's version of the inserted text inside the field.
- `diff` compares it with what was typed: words swapped ("Ganesh" -> "Gyanesh"), deleted, added.
  Swaps go to the corrections/vocabulary loop (api/routes.py).
- `preferences` counts, over recent edits, words the user keeps deleting (cut them) and words they
  put back after the cleanup took them out (keep them). Both become hints for the LLM pass.
- `examples` picks recent Ramble takes the user edited, as (what was said, what they sent) pairs:
  their own rewrites, shown to the LLM ahead of the take. Only pairs that pass ramble.check (the
  user didn't add facts that weren't said), so the examples never teach it to invent.
"""

from __future__ import annotations

import difflib
import json
from collections import Counter
from dataclasses import dataclass, field

from .guard import PROTECTED
from .text import normalize_key, tokenize
from .vocab.matcher import COMMON_WORDS

MAX_SWAP_WORDS = 3  # a swap longer than this is a rewrite, not a misheard word
MIN_CUT_TIMES = 3  # a word deleted this many times (in 2+ dictations) is one they don't want
MIN_KEEP_TIMES = 2  # a word put back this many times is one the cleanup must not cut
RECENT_EDITS = 200


def _words(text: str) -> list[str]:
    return text.split()


def _keys(words: list[str]) -> list[str]:
    return [normalize_key(w) for w in words]


def locate(inserted: str, field_text: str) -> str:
    """The part of `field_text` that is the user's version of `inserted` (the field may hold text
    before and after it). Matched by words: from the first to the last word that lines up with the
    inserted text, widened by the inserted words at either end that no longer match (edited)."""
    ins, fld = _words(inserted), _words(field_text)
    if not ins or not fld:
        return field_text.strip()
    blocks = [b for b in difflib.SequenceMatcher(None, _keys(ins), _keys(fld), autojunk=False).get_matching_blocks()
              if b.size]
    if not blocks:
        return field_text.strip()
    first, last = blocks[0], blocks[-1]
    lo, hi = first.b, last.b + last.size  # surely the dictation's
    head, tail = first.a, len(ins) - last.a - last.size  # inserted words outside the matches (edited)
    target = " ".join(_keys(ins))

    def score(s: int, e: int) -> float:
        return difflib.SequenceMatcher(None, target, " ".join(_keys(fld[s:e])), autojunk=False).ratio()

    # The edited ends may have more or fewer words than were inserted: of the spans reaching up to
    # one word further than that, the one that reads most like the inserted text.
    start = max(range(max(0, lo - head - 1), lo + 1), key=lambda s: (score(s, hi), s)) if head else lo
    end = max(range(hi, min(len(fld), hi + tail + 1) + 1), key=lambda e: (score(start, e), -e)) if tail else hi
    return " ".join(fld[start:end])


@dataclass
class Diff:
    swaps: list[tuple[str, str]] = field(default_factory=list)  # (typed, user's) short replacements
    deleted: list[str] = field(default_factory=list)  # keys
    added: list[str] = field(default_factory=list)  # keys

    @property
    def changed(self) -> bool:
        return bool(self.swaps or self.deleted or self.added)


def diff(typed: str, edited: str) -> Diff:
    """Word-level changes from `typed` to `edited`, ignoring case and punctuation."""
    a, b = _words(typed), _words(edited)
    ka, kb = _keys(a), _keys(b)
    out = Diff()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if op == "replace" and i2 - i1 <= MAX_SWAP_WORDS and j2 - j1 <= MAX_SWAP_WORDS:
            out.swaps.append((" ".join(a[i1:i2]).strip(".,!?;:"), " ".join(b[j1:j2]).strip(".,!?;:")))
            continue
        if op in ("replace", "delete"):
            out.deleted += [k for k in ka[i1:i2] if k]
        if op in ("replace", "insert"):
            out.added += [k for k in kb[j1:j2] if k]
    return out


@dataclass
class Preferences:
    cut: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)

    def hints(self) -> str:
        lines = []
        if self.cut:
            lines.append("Words they always delete afterwards (cut them): " + ", ".join(self.cut) + ".")
        if self.keep:
            lines.append("Words they put back when they were cut (keep them): " + ", ".join(self.keep) + ".")
        return "\n".join(lines)


def preferences(edits: list[tuple[str, str, str]]) -> Preferences:
    """From (raw_text, typed, edited) triples: words deleted MIN_CUT_TIMES+ times across 2+ takes,
    and words put back (said, cut by the cleanup, added again by the user) MIN_KEEP_TIMES+ times."""
    cut, cut_takes, keep = Counter(), Counter(), Counter()
    for raw, typed, edited in edits:
        d = diff(typed, edited)
        for k in d.deleted:
            cut[k] += 1
        for k in set(d.deleted):
            cut_takes[k] += 1
        said, shown = Counter(_keys(_words(raw))), Counter(_keys(_words(typed)))
        for k in d.added:
            if said[k] > shown[k]:
                keep[k] += 1
    never = PROTECTED  # negations, time words: never suggest cutting them
    return Preferences(
        cut=sorted(k for k, n in cut.items() if n >= MIN_CUT_TIMES and cut_takes[k] >= 2 and k not in never
                   and keep[k] == 0),
        keep=sorted(k for k, n in keep.items() if n >= MIN_KEEP_TIMES and k not in COMMON_WORDS - {"like"}),
    )


def examples(rows: list[tuple[str, str, str]], n: int = 3) -> list[tuple[str, str]]:
    """Recent Ramble takes the user edited, newest first: (said, sent) pairs that pass the fact
    check against what was said and are a real rewrite of it (not a fresh message)."""
    from .ramble import check

    out: list[tuple[str, str]] = []
    for raw, typed, edited in rows:
        if len(out) >= n:
            break
        if not edited.strip() or edited.strip() == typed.strip() or len(_words(raw)) < 15:
            continue
        if check(raw, edited):
            continue
        out.append((raw, edited))
    return out


def ramble_rows(db, limit: int = RECENT_EDITS) -> list[tuple[str, str, str, str]]:
    """(raw_text, typed, edited, style) for the latest edit of each recent dictation, newest first."""
    from sqlmodel import select

    from .db import Dictation, DictationEdit

    seen: set[int] = set()
    rows = []
    with db.session() as s:
        q = select(DictationEdit, Dictation).where(DictationEdit.dictation_id == Dictation.id) \
            .order_by(DictationEdit.id.desc()).limit(limit)
        for e, d in s.exec(q).all():
            if e.dictation_id in seen:
                continue
            seen.add(e.dictation_id)
            style = json.loads(d.result_json or "{}").get("style", "")
            rows.append((d.raw_text, e.inserted_text or d.text, e.edited_text, style))
    return rows


def tokens_equal(a: str, b: str) -> bool:
    return [t.key for t in tokenize(a) if t.key] == [t.key for t in tokenize(b) if t.key]
