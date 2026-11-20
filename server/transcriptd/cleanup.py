"""Deterministic cleanup: the only edits are ones the guard can verify.

This module never calls a language model. It produces a candidate; the guard decides
what survives. Each rule is a toggle so users can turn the pipeline down to
"verbatim + punctuation" if they want.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .guard import FILLERS, LIST_JOINERS, PROTECTED
from .text import Token, detokenize, tokenize

_TERMINAL = ".?!।"


@dataclass(slots=True)
class CleanupOptions:
    remove_fillers: bool = True
    collapse_stutters: bool = True
    capitalize_first: bool = True
    ensure_terminal_punctuation: bool = False  # off: a dictated fragment is often mid-sentence
    layout: str = "none"  # none | note | email (see format_layout)


def cleanup(raw_text: str, opts: CleanupOptions | None = None) -> str:
    opts = opts or CleanupOptions()
    tokens = tokenize(raw_text)
    if opts.remove_fillers:
        tokens = _remove_fillers(tokens)
    if opts.collapse_stutters:
        tokens = _collapse_stutters(tokens)
    text = detokenize(tokens)
    if opts.capitalize_first:
        text = _capitalize_first(text)
    if opts.ensure_terminal_punctuation and text and text[-1] not in _TERMINAL:
        text += "."
    return format_layout(text, opts.layout)


def _remove_fillers(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    for t in tokens:
        if t.key in FILLERS:
            # Keep a trailing comma/period the filler carried, attached to the previous word.
            trailing = t.surface[len(t.surface.rstrip(",.?!।")):]
            if trailing and out and out[-1].surface[-1] not in ",.?!।":
                prev = out[-1]
                out[-1] = Token(surface=prev.surface + trailing, key=prev.key)
            continue
        out.append(t)
    return out


def _collapse_stutters(tokens: list[Token]) -> list[Token]:
    """'I I think the the plan' -> 'I think the plan'. Protected words are never collapsed."""
    out: list[Token] = []
    for t in tokens:
        if out and not t.is_punct and out[-1].key == t.key and t.key not in PROTECTED:
            # Keep the later token: it usually carries the punctuation.
            out[-1] = t
            continue
        out.append(t)
    return out


def _capitalize_first(text: str) -> str:
    for i, ch in enumerate(text):
        if ch.isalpha():
            if ch.isascii():
                return text[:i] + ch.upper() + text[i + 1 :]
            return text  # Devanagari has no case
        if not ch.isspace() and ch not in "\"'([{":
            return text
    return text


# -- layout styles ------------------------------------------------------------------------
# Deterministic, and built on the punctuation the ASR model already produced: only line
# breaks, bullets, colons, commas and capitals are added. The one word ever dropped is the
# "and" joining the last list item, which the guard allows only right before a bullet.

_SENTENCE = re.compile(r"(?<=[.?!।])\s+")
_TOPIC = re.compile(r"^(also|additionally|separately|another thing|on another note|besides that|secondly|thirdly|"
                    r"finally|lastly|anyway|by the way|oh and|ps)\b", re.IGNORECASE)
_PRONOUN = {"i", "we", "you", "he", "she", "they", "it", "that", "this", "there", "मैं", "हम", "मी", "आम्ही"}
_MAX_ITEM = 4  # words; anything longer is a clause, not a list item
_GREETING = re.compile(r"^((?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b[^,.!?\n]{0,40}[,!])\s+(.+)$",
                       re.IGNORECASE | re.DOTALL)
_SIGNOFF = re.compile(r"(?:^|(?<=[.?!]\s))((?:thanks|thank you|many thanks|regards|best regards|kind regards|warm regards|"
                      r"best|cheers|sincerely|take care)(?: so much)?)([,.!]?)((?:\s+(?-i:[A-Z])[\w'-]*){0,3})\.?$", re.IGNORECASE)


def format_layout(text: str, layout: str) -> str:
    if not text or layout == "none":
        return text
    if layout == "note":
        return _note(text)
    if layout == "email":
        return _email(text)
    return text


def _cap(s: str) -> str:
    return _capitalize_first(s)


def _paragraphs(sentences: list[str], per_paragraph: int = 0) -> list[str]:
    """Group sentences, starting a new paragraph at a topic change ("Also, ...")."""
    paras: list[list[str]] = []
    for sent in sentences:
        if not paras or _TOPIC.match(sent) or (per_paragraph and len(paras[-1]) >= per_paragraph):
            paras.append([])
        paras[-1].append(sent)
    return [" ".join(p) for p in paras]


def _note(text: str) -> str:
    blocks: list[str] = []
    prose: list[str] = []

    def flush() -> None:
        if prose:
            blocks.extend(_paragraphs(prose))
            prose.clear()

    for sent in _SENTENCE.split(text.strip()):
        found = _find_list(sent)
        if not found:
            prose.append(sent)
            continue
        lead, items, rest = found
        flush()
        bullets = "\n".join("• " + item for item in items)
        blocks.append(f"{_cap(lead)}:\n{bullets}" if lead else bullets)
        if rest:
            prose.append(_cap(rest))
    flush()
    return "\n\n".join(blocks)


def _find_list(sentence: str) -> tuple[str, list[str], str] | None:
    """'I need to buy milk, eggs and bread, and then X.' ->
    ('I need to buy', ['milk', 'eggs', 'bread'], 'and then X.'). None unless it is clearly a list:
    three or more short comma-separated items, the last joined by "and"."""
    body = sentence.rstrip()
    end = body[-1] if body and body[-1] in _TERMINAL else ""
    body = body[: len(body) - len(end)] if end else body
    segs = [x.strip() for x in body.split(",")]
    if len(segs) < 2:
        return None
    for start in range(len(segs) - 1):
        for stop in range(start + 1, len(segs)):
            middle = segs[start + 1 : stop]
            if not all(_short(m) for m in middle):
                break
            words = segs[stop].split()
            joins = [n for n, w in enumerate(words) if w.casefold() in LIST_JOINERS]
            if not joins:
                continue
            j = joins[0]
            before, after = " ".join(words[:j]), " ".join(words[j + 1 :])
            if j == 0:  # Oxford comma: "..., eggs, and bread"
                if not middle or not _short(after):
                    continue
                last = [after]
            elif _short(before) and _short(after):
                last = [before, after]
            else:
                continue
            # The first item is as long as its neighbour: "buy milk, eggs and a sourdough loaf"
            # keeps "buy" in the lead-in rather than in the item.
            first_words = segs[start].split()
            width = _first_width(first_words, min(len((middle or last)[0].split()), len(first_words)))
            lead_words = first_words[: len(first_words) - width]
            if len(first_words) < width or (len(lead_words) == 1 and _head(lead_words[0]) in _PRONOUN):
                continue  # "I went home, ate and slept" is not a list
            first = " ".join(first_words[-width:])
            if not _short(first) or len(middle) + len(last) + 1 < 3:
                continue
            lead = ", ".join([*segs[:start], " ".join(lead_words)]).strip(", ")
            rest = ", ".join(segs[stop + 1 :]).strip()
            rest = rest + end if rest else ""
            return lead, [first, *middle, *last], rest
    return None


# Words that introduce a list ("buy milk, ...", "need eggs, ...", "for Raj, ..."), and words an
# item never starts with past one of them ("to", modals, pronouns).
_LEAD_INS = {
    "buy", "get", "grab", "bring", "pick", "pack", "order", "need", "needs", "want", "wants", "include", "includes",
    "including", "like", "likes", "love", "loves", "have", "has", "had", "are", "is", "was", "were", "be", "call",
    "email", "text", "invite", "visit", "see", "meet", "cook", "make", "take", "pay", "check", "clean", "finish",
    "add", "discuss", "discussed", "cover", "review", "remember", "with", "for", "of", "about", "from", "between",
}
_NOT_IN_ITEM = {"to", "and", "or", "but", "should", "will", "would", "can", "could", "must", "may", "might", "also", "then"}


def _first_width(first_words: list[str], width: int) -> int:
    """The neighbour's width, widened over modifiers when a lead-in sits just before them:
    "buy oat milk, eggs and bread" -> "oat milk", not "milk" after "buy oat"."""
    w = width
    while w < min(_MAX_ITEM, len(first_words)):
        before = first_words[-w - 1].casefold().rstrip(":")
        if before in _LEAD_INS:
            return w
        if before in _NOT_IN_ITEM or before in _PRONOUN or not before.isascii():
            break
        w += 1
    if w < len(first_words) and first_words[-w - 1].casefold().rstrip(":") in _LEAD_INS:
        return w
    return width


def _short(seg: str) -> bool:
    return 0 < len(seg.split()) <= _MAX_ITEM and _head(seg) not in _PRONOUN


def _head(seg: str) -> str:
    """First word without a contraction: "it's" -> "it", so clauses are not list items."""
    return re.split(r"['’]", seg.split()[0].casefold())[0]


def _email(text: str) -> str:
    text = text.strip()
    greeting = signoff = ""
    if m := _GREETING.match(text):
        greeting, text = m.group(1), _cap(m.group(2))
    if m := _SIGNOFF.search(text):
        phrase, punct, name = m.group(1), m.group(2), m.group(3).strip()
        body = text[: m.start()].rstrip()
        if body:
            text = body
            signoff = _cap(phrase) + ("," if name else punct or "") + ("\n" + name if name else "")
    body = "\n\n".join(_paragraphs(_SENTENCE.split(text), per_paragraph=3)) if text else ""
    return "\n\n".join(x for x in (greeting, body, signoff) if x)
