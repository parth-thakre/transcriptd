"""Use the text around the caret in the target field ("context") to help a dictation.

Three uses, all conservative:
- document vocabulary: names and identifiers already in the field become temporary
  vocabulary for this dictation (post-hoc matching only; no prompt biasing);
- smart join: leading/trailing space and first-letter case decided from what the text
  is being inserted between;
- profile by script: a Devanagari document routes to a multilingual profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .guard import PROTECTED
from .text import normalize_key, script_of

_WORD = re.compile(r"[\w'\-]+", re.UNICODE)
_IDENT = re.compile(r"^(?:[A-Za-z]+(?:[A-Z][a-z0-9]+)+|[a-z0-9]+(?:_[a-z0-9]+)+|[A-Za-z]+\d+[A-Za-z0-9]*|[A-Z]{2,}[a-z0-9]*)$")
_SENTENCE_END = ".!?।\n"
_OPENERS = "([{\"'“‘"


@dataclass(slots=True)
class DictationContext:
    app: str = ""
    before: str = ""  # text before the caret (tail)
    after: str = ""  # text after the caret (head)
    languages_hint: list[str] = field(default_factory=list)
    caret: bool = True  # False: only the app is known, not the text around the caret

    @classmethod
    def from_dict(cls, d: dict | None) -> "DictationContext | None":
        if not d:
            return None
        return cls(app=str(d.get("app") or ""), before=str(d.get("before") or "")[-2000:], after=str(d.get("after") or "")[:500])

    @classmethod
    def with_app(cls, ctx: "DictationContext | None", app: str | None) -> "DictationContext | None":
        """`ctx` with the client's app hint filled in, or a context that only knows the app."""
        if not app:
            return ctx
        if ctx is None:
            return cls(app=app, caret=False)
        ctx.app = ctx.app or app
        return ctx


def is_chat_app(app: str | None, chat_apps: list[str]) -> bool:
    a = (app or "").casefold()
    return bool(a) and any(c.casefold() in a for c in chat_apps)


def document_terms(ctx: DictationContext, max_terms: int = 40) -> list[str]:
    """Words in the field worth treating as vocabulary: capitalised words that are not
    at a sentence start, code-like identifiers, and Devanagari words. Protected words
    and very short tokens are skipped."""
    text = ctx.before + " " + ctx.after
    out: list[str] = []
    seen: set[str] = set()
    tokens = list(_WORD.finditer(text))
    for i, m in enumerate(tokens):
        w = m.group(0)
        k = normalize_key(w)
        if not k or k in seen or k in PROTECTED or len(k) < 3:
            continue
        prev_end = tokens[i - 1].end() if i else 0
        between = text[prev_end : m.start()]
        sentence_start = i == 0 or any(c in _SENTENCE_END for c in between)
        kind = script_of(w)
        is_name = kind == "latin" and w[0].isupper() and not sentence_start and not w.isupper()
        is_ident = kind in ("latin", "mixed") and bool(_IDENT.match(w))
        is_deva = kind == "deva" and len(w) >= 3
        if is_name or is_ident or is_deva:
            seen.add(k)
            out.append(w)
            if len(out) >= max_terms:
                break
    return out


def profile_for_context(ctx: DictationContext, default_profile: str, profiles: dict) -> str:
    """If the field is mostly Devanagari and a multilingual profile exists, use it."""
    text = (ctx.before[-400:] + ctx.after[:100]).strip()
    if not text:
        return default_profile
    words = _WORD.findall(text)
    deva = sum(1 for w in words if script_of(w) == "deva")
    if words and deva / len(words) >= 0.3:
        for name, p in profiles.items():
            langs = getattr(p, "languages", [])
            if "mr" in langs or "hi" in langs:
                return name
    return default_profile


def smart_join(text: str, ctx: DictationContext | None, raw_first_word: str | None = None) -> str:
    """Return the text to insert: spacing and first-letter case fitted to the caret."""
    if not text:
        return text
    if ctx is None or not ctx.caret:
        return text
    before = ctx.before
    after = ctx.after
    out = text
    # First-letter case: mid-sentence -> lowercase unless the raw ASR itself capitalised it
    # (proper noun / vocab term) or the word is an acronym/identifier.
    at_start = not before.strip() or before.rstrip(" \t").endswith(tuple(_SENTENCE_END)) or before.rstrip(" \t")[-1] in _OPENERS
    first = out[0]
    if not at_start and first.isupper() and first.isascii():
        raw_cap = bool(raw_first_word) and raw_first_word[0].isupper()
        acronym = len(out) > 1 and out[1].isupper()
        if not raw_cap and not acronym:
            out = first.lower() + out[1:]
    # Leading space when gluing onto a word or punctuation that is not an opener.
    if before and not before[-1].isspace() and before[-1] not in _OPENERS:
        out = " " + out
    # Trailing space when the next character is a letter/digit right at the caret.
    if after and (after[0].isalnum() or script_of(after[0]) == "deva"):
        out = out + " "
    elif not after and before.strip():
        out = out + " "  # end of text: leave room for the next word
    elif not after and not before.strip():
        out = out + " "
    return out
