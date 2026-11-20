"""Script-agnostic phonetic keys so a custom term matches however the ASR spelled it.

"Kubernetes", "cooper netties" and "कुबरनेटीस" should all land near the same key.
Latin text goes straight to Metaphone. Devanagari is transliterated to IAST, folded to
ASCII, then keyed the same way.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import jellyfish
from indic_transliteration import sanscript

from ..text import normalize_key, script_of

_NON_ALPHA = re.compile(r"[^a-z]+")


def _ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


@lru_cache(maxsize=65536)
def romanize(word: str) -> str:
    """Devanagari -> rough Latin; Latin passes through lowercased."""
    kind = script_of(word)
    if kind == "deva" or kind == "mixed":
        latin = sanscript.transliterate(word, sanscript.DEVANAGARI, sanscript.IAST)
        latin = _ascii_fold(latin)
        # IAST writes the inherent vowel explicitly (rāma -> rama). Hindi/Marathi drop
        # the final schwa in speech, so strip a trailing 'a' after a consonant.
        if len(latin) > 2 and latin.endswith("a") and latin[-2] not in "aeiou":
            latin = latin[:-1]
        return latin
    return _ascii_fold(word)


@lru_cache(maxsize=65536)
def phonetic_key(text: str) -> str:
    """Metaphone of the romanized, space-joined tokens. Empty string if nothing keyable."""
    parts = []
    for tok in text.split():
        r = _NON_ALPHA.sub("", romanize(normalize_key(tok)))
        if r:
            parts.append(jellyfish.metaphone(r))
    return " ".join(p for p in parts if p)


def keys_for_term(term: str, aliases: list[str] = ()) -> set[str]:
    """All phonetic keys a term may appear as, including the joined (no-space) form so
    'cooper netties' can match 'Kubernetes'."""
    out = set()
    for form in [term, *aliases]:
        k = phonetic_key(form)
        if k:
            out.add(k)
            out.add(k.replace(" ", ""))
    return out
