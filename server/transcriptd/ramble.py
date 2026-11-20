"""Ramble mode: the LLM rewrites a spoken ramble into what the speaker meant, and this module
checks the rewrite against what was said.

The word-level guard (guard.py) can't verify a rewrite: new wording is the point. So the check
here is about facts, not words. A rewrite passes when:

- it invents nothing: every number, name and time word in it was said;
- it flips nothing: where an output sentence keeps a stretch of what was said, a negation, time
  word, number or name in that stretch is still there (and no negation appears that wasn't); a
  negation next to a word the rewrite kept is still there, even at the edge of a sentence ("don't
  deploy" -> "Deploy"); a word said only under a negation never comes back without one ("we don't
  deploy on Fridays" -> "Fridays are for deploying"); there are no more negations than were said;
  no word turns into its opposite ("no" -> "yes", "demand" -> "refuse") unless that was said; and
  where both of a pair were said, neither lands in a clause with a word said only in its
  partner's clause ("the heater is on, the fan is off" never becomes "the heater is off, the fan
  is on"), and unless each keeps the words right after it, the rewrite says them in the order
  said ("reject the first, approve the last" never becomes "approve the first, reject the last").
  Clauses are read off punctuation and joining words, so this is a heuristic: an opposite moved
  to a clause it shares no telling word with is not caught;
- it keeps conditions: where a sentence keeps words said next to "if", "unless", "as long as"
  (guard.CONDITIONS), it says a condition of its own too ("approve it if they agree" is never
  "approve it; they agree"), in a clause that says what the said one did, unless the whole
  clause around it was dropped. An "unless" stays
  "unless": one written as "if ... not" fails, as the check can't tell which "if" the "not"
  went to (a false reject only falls back to the guarded cleanup);
- it condenses: it is not longer than what was said.

Whole stretches the rewrite leaves out (a tangent they abandoned, a point said twice, thinking
aloud) are allowed: dropping those is what Ramble is for. Anything else fails, and the pipeline
falls back to the usual guarded cleanup.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

from .guard import CONDITIONS, words_to_number
from .text import SIGNS, join_marks, normalize_key
from .vocab.matcher import COMMON_WORDS

NEGATIONS = frozenset(normalize_key(w) for w in [
    "not", "no", "never", "none", "nothing", "nobody", "nowhere", "neither", "nor", "cannot", "without",
    "don't", "doesn't", "didn't", "won't", "wouldn't", "can't", "couldn't", "shouldn't", "isn't", "aren't",
    "wasn't", "weren't", "hasn't", "haven't", "hadn't", "mustn't", "ain't", "नहीं", "नाही", "मत", "नको",
    # Hindi and Marathi as ASR romanizes them ("mat karo deploy"); not "na", which is also a tag
    "nahi", "nahin", "nahee", "nai", "mat", "nako", "naka", "nahiye",
    # ...and as ASR often writes them, without the apostrophe
    "dont", "doesnt", "didnt", "wont", "wouldnt", "cant", "couldnt", "shouldnt", "isnt", "arent", "wasnt",
    "werent", "hasnt", "havent", "hadnt", "mustnt", "aint",
])
TIME_WORDS = frozenset(normalize_key(w) for w in [
    "before", "after", "until", "till", "already", "yet", "today", "tonight", "tomorrow", "yesterday",
    "morning", "afternoon", "evening", "night", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december", "week", "weekend", "month", "year", "now", "later", "soon", "earlier",
])
# Said right after something to take it back ("DS224, no wait, DS223"; "Tuesday, sorry, Monday").
# Not "actually"/"rather": "I don't actually want it" must still count as a negation.
CORRECTION_CUES = frozenset(["sorry", "wait", "mean", "scratch", "oops", "correction"])
_NUMBER_WORDS = frozenset([
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million", "billion",
])
# Capitalised words that are not names: sentence openers that start lists, pronouns, interjections.
_NOT_NAMES = frozenset(["i", "i'm", "i've", "i'll", "i'd", "ok", "okay"])
# Hyphens split ("8-year-old" is 8 year old), except in an exponent: "1e-3" is one number.
# A "+" may be inside a word ("C++") but never starts one: "+5" is a sign and a number.
_WORD = re.compile(r"(?:\d*\.)?\d+[eE][-+−]?\d+(?!\w)|[\w'’.][\w'’.+]*|[^\w\s]", re.UNICODE)
_SIGN = "".join(sorted(set(SIGNS.values())))  # what a number's sign keys as: "-", "+", "~", "<", "±"...
_DIGITS = re.compile(rf"(?:(?<![\w.])[{re.escape(_SIGN)}]+)?(?:\d+|(?=\.\d))(?:[.:,]\d+)*(?:[eE][-+−]?\d+)?[%‰]?")
_MINUS = frozenset(["minus", "negative"])
# A number's unit, said or written: "$5", "5 dollars" and "five dollars" are all "$5"; "5 °" and
# "5 degrees" are "5°". A unit changed or dropped is a number not said.
_CURRENCY = {"$": "$", "€": "€", "£": "£", "₹": "₹", "¥": "¥", "₨": "₹"}
_UNIT_WORDS = {
    **dict.fromkeys(["percent"], "%"), **dict.fromkeys(["degree", "degrees"], "°"),
    **dict.fromkeys(["dollar", "dollars"], "$"), **dict.fromkeys(["euro", "euros"], "€"),
    **dict.fromkeys(["rupee", "rupees", "rs"], "₹"), **dict.fromkeys(["yen"], "¥"),
    # A unit of its own, as it may be weight or money: "5 pounds" is neither "5" nor "£5"
    **dict.fromkeys(["pound", "pounds"], "pound"),
}
_CLAUSE_BREAKS = frozenset(",;:.?!…—–")

# Words a negation's reach is not about: "I don't think it works" -> "I think it doesn't work"
# moves the "not", it doesn't flip anything; the rest is grammar.
_NOT_SCOPE = frozenset([
    "i", "me", "my", "you", "your", "we", "us", "our", "he", "him", "his", "she", "her", "it", "its", "they",
    "them", "their", "a", "an", "the", "this", "that", "these", "those", "to", "of", "in", "on", "at", "for",
    "by", "with", "from", "as", "about", "into", "and", "or", "but", "so", "if", "because", "is", "are", "was",
    "were", "be", "been", "am", "do", "does", "did", "have", "has", "had", "will", "would", "can", "could",
    "should", "shall", "may", "might", "must", "get", "got", "go", "really", "even", "actually", "just", "too",
    "very", "much", "any", "anything", "anyone", "anybody", "anymore", "ever", "at", "all", "like", "um", "uh",
    "think", "believe", "suppose", "guess", "say", "said", "mean", "one", "there", "here", "what",
])
# A negation reaches to the end of its clause: punctuation, or one of these. "do not under any
# circumstances share the password" negates the sharing.
_CLAUSE_WORDS = frozenset([
    "and", "but", "or", "because", "cause", "since", "so", "while", "whereas", "although", "though", "when",
    "whenever", "if", "unless", "then", "until", "till", "before", "after",
])
# ...except that "and", "or" and "nor" carry it on to what they join ("don't email or call the
# client" negates the call too), unless a new subject starts there ("I don't like it and I'm
# leaving", "it's not raining and the car is fine").
_JOINERS = frozenset(["and", "or", "nor"])
_SUBJECTS = frozenset([
    "i", "i'm", "i'll", "i'd", "i've", "im", "ill", "ive", "we", "we're", "we'll", "we've", "we'd", "you", "you're",
    "you'll", "you've", "you'd", "youre", "he", "he's", "he'll", "she", "she's", "she'll", "it", "it's", "it'll",
    "its", "they", "they're", "they'll", "they've", "theyre", "there", "there's", "theres", "that's", "thats",
    "this", "that", "these", "those", "the", "a", "an", "my", "our", "your", "his", "her", "their", "everyone",
    "everybody", "someone", "somebody", "nobody", "no", "nothing", "everything", "something",
])
# Where "mat" is the English noun ("a yoga mat"), not the Hindi "don't".
_MAT_NOUN = frozenset(["a", "the", "my", "your", "our", "his", "her", "their", "this", "that", "yoga", "door",
                       "bath", "mouse", "floor", "exercise", "gym"])

# Words that say the opposite of each other. A rewrite may use one only where it was said, or its
# opposite wasn't: "we demand a refund" is never "we refuse a refund". Each side lists base forms;
# their inflections ("refused", "stopping") count as the word.
_OPPOSITES = [
    ("yes yeah yep yup", "no nope nah"), ("demand", "refuse decline"), ("accept", "reject refuse decline"),
    ("allow permit", "deny block forbid prevent ban"), ("approve", "reject deny decline"),
    ("include", "exclude"), ("enable", "disable"), ("start begin", "stop finish"), ("increase", "decrease reduce"),
    ("add", "remove delete"), ("buy", "sell"), ("win", "lose"), ("open", "close shut"), ("before", "after"),
    ("true", "false"), ("pass", "fail"), ("succeed success", "fail failure"), ("more", "less fewer"),
    ("maximum max", "minimum min"), ("high higher", "low lower"), ("early earlier", "late later"),
    ("first", "last"), ("agree", "disagree"), ("love", "hate"), ("always", "never"), ("possible", "impossible"),
    ("likely", "unlikely"), ("able", "unable"), ("correct", "incorrect wrong"), ("positive", "negative"),
    ("upload", "download"), ("import", "export"), ("push", "pull"), ("remember", "forget"),
    ("public", "private"), ("online", "offline"), ("internal", "external"), ("legal", "illegal"),
    ("including", "except excepting"), ("on", "off"), ("up", "down"),
]
# "on", "off", "up" and "down" are mostly prepositions; they are each other's opposites only as
# the particle of one of these verbs ("turn it off", "prices went up", "scale down").
_PARTICLES = frozenset(["on", "off", "up", "down"])
_PARTICLE_VERBS = frozenset([
    "turn", "switch", "power", "flip", "toggle", "log", "sign", "go", "goes", "went", "gone", "move", "scale",
    "ramp", "dial", "crank", "slow", "speed", "trend", "count", "level", "push", "bump", "mark", "price", "rate",
    "round", "vote", "step", "tune", "spin", "shoot",
    # ...and the state ones: "the lights are off", "keep the heater off"
    "is", "are", "was", "were", "keep", "kept", "leave", "left", "stay",
])
_IRREGULAR = {"win": "won winning", "lose": "lost", "buy": "bought", "sell": "sold", "begin": "began begun",
              "forget": "forgot forgotten", "shut": "shutting", "stop": "stopped stopping"}


@dataclass(slots=True)
class _Tok:
    surface: str
    key: str
    starts_sentence: bool
    starts_clause: bool = False  # after a comma, colon, dash or a sentence end
    unit: str = ""  # a number's currency or degree sign, written next to it ("$5", "5 °", "$ one")


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    start = True
    for line in text.split("\n"):
        # A number's marks written apart are part of it: "$- 5" is "$-5", "- 5 %" is "-5%".
        line = " ".join(g.replace(" ", "") for g in join_marks(line.split(), ranges=True))
        line = re.sub(r"^\s*(?:[-•*]|\d{1,2}[.)])\s+", "", line)  # list markers are layout
        start = clause = True
        last, n0 = 0, len(out)
        for m in _WORD.finditer(line):
            s = m.group(0)
            if not re.search(r"\w", s):
                if len(out) > n0 and out[-1].surface[-1:].isdigit() and not line[last:m.start()].strip():
                    if s in "%‰":
                        out[-1].surface += s  # "5%", "5 %": the number's unit
                        out[-1].key = normalize_key(out[-1].surface)
                        continue
                    if (s == "°" or unicodedata.category(s) == "Sc") and not out[-1].unit \
                            and not re.match(r"\s*\S?\.?\d", line[m.end():]):  # "5°", "5 €"; not "3 $5"
                        out[-1].unit = _CURRENCY.get(s, s)
                        continue
                if s in ".?!:;":
                    start = True
                clause = clause or s in _CLAUSE_BREAKS
                continue
            a = m.start()
            unit = ""
            if re.match(r"\.?\d", s):  # its marks, written on to it ("-$5", "$-5", "+$5", "-%5"; not "F-16")
                b, sign, cur, pct = a, "", "", ""
                while b and (line[b - 1] in SIGNS or line[b - 1] in "%‰°" or unicodedata.category(line[b - 1]) == "Sc"):
                    if unicodedata.category(line[b - 1]) != "Sc" and b > 1 and line[b - 2].isalnum():
                        break  # a hyphen, not a sign: "F-16", "3-5"; another number's unit: "5%5"
                    b -= 1
                    if line[b] in SIGNS:
                        sign = SIGNS[line[b]] + sign
                    elif line[b] in "%‰":
                        pct = line[b] + pct
                    else:
                        cur = line[b] + cur
                body = s.rstrip(".")
                unit, s = _CURRENCY.get(cur, cur), sign + body + pct + s[len(body):]  # "-%5." is "-5%."
            elif normalize_key(s) in _NUMBER_WORDS:  # "$ one", "€five"
                b = len(line[:a].rstrip())
                while b and unicodedata.category(line[b - 1]) == "Sc":
                    b -= 1
                cur = line[b:len(line[:a].rstrip())]
                unit = _CURRENCY.get(cur, cur)
            s = s if re.match(rf"[{re.escape(_SIGN)}]*\.\d", s) else s.lstrip(".")  # ".5" is 0.5, never 5
            k = normalize_key(s.rstrip(".").replace("’", "'"))
            if k:
                out.append(_Tok(s.rstrip("."), k, start, clause or start, unit))
                last = m.end()
                start = s.endswith((".", "?", "!"))
                clause = s.endswith(".")
    return out


def _numbers(toks: list[_Tok], lone_one: bool = False) -> list[tuple[int, str]]:
    """(token index, value) of every number: digits anywhere in a token ("8GB", "M1", "7th"),
    and spoken numbers ("twenty six"; "one two three" is three numbers). A lone "one" counts
    only with `lone_one` ("one big reason" is usually not a number) or a unit ("one dollar")."""
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(toks):
        if toks[i].key in _NUMBER_WORDS:
            j = i
            while j < len(toks) and (toks[j].key in _NUMBER_WORDS or (toks[j].key == "and" and j > i)):
                j += 1
            while j > i and toks[j - 1].key == "and":
                j -= 1
            for a, b in _split_run([t.key for t in toks[i:j]]):
                keys = [t.key for t in toks[i + a:i + b]]
                for c, d, n in _pieces(keys):
                    minus = a + c == 0 and i > 0 and toks[i - 1].key in _MINUS  # "minus five" is -5
                    unit = _unit_word(toks, i + a + d) if i + a + d == j else ""  # "five dollars" is $5
                    unit = toks[i + a + c].unit or unit  # "$ five"
                    if lone_one or unit or keys[c:d] != ["one"]:  # "one dollar" is $1
                        out.append((i + a + c, _with_unit(f"-{n}" if minus else str(n), unit)))
            i = j
            continue
        found = _DIGITS.findall(toks[i].surface)
        for n, d in enumerate(found):
            d = re.sub(r"[eE]\+?", "e", d.replace(",", "").replace("−", "-"))  # "1E+3" is 1e3; "1e-3" is not
            d = re.sub(rf"^([{re.escape(_SIGN)}]*)\.", r"\g<1>0.", d)  # ".5" = "0.5"
            if (u := toks[i].unit) and n == (len(found) - 1 if u == "°" else 0):
                d = _with_unit(d, toks[i].unit)  # "$5", "5°"
            if n == len(found) - 1 and not d.endswith(("%", "‰")) and (u := _unit_word(toks, i + 1)):
                d = _with_unit(d, u)  # "5 percent", "5 dollars"
            out.append((i, d))
        i += 1
    return out


def _unit_word(toks: list[_Tok], i: int) -> str:
    """The unit toks[i] says, if it is one ("percent" -> "%", "dollars" -> "$")."""
    return _UNIT_WORDS.get(toks[i].key, "") if i < len(toks) else ""


def _with_unit(v: str, unit: str) -> str:
    """A number's value with its unit: a currency after its sign ("-$5"), any other after it."""
    if not unit or unit in v:
        return v
    if unit.isalpha():
        return f"{v} {unit}"  # "5 pound"
    if unit in "%‰°":
        return v + unit
    n = len(v) - len(v.lstrip(_SIGN))
    return v[:n] + unit + v[n:]


def _stem(k: str) -> str:
    """Rough inflection fold: "deploying", "deployed", "deploys" -> "deploy"; "shipped" -> "ship"."""
    for suf in ("ing", "ed", "es", "s", "e"):
        if k.endswith(suf) and len(k) - len(suf) >= 3:
            k = k[:-len(suf)]
            break
    if len(k) >= 4 and k[-1] == k[-2] and k[-1] not in "aeiouls":
        k = k[:-1]
    return k


def _negation(toks: list[_Tok], j: int) -> bool:
    """toks[j] negates what follows: not the "no" of "DS224, no wait, DS223", nor a yoga mat."""
    if toks[j].key == "mat" and j and toks[j - 1].key in _MAT_NOUN:
        return False
    return toks[j].key in NEGATIONS and not (j + 1 < len(toks) and toks[j + 1].key in CORRECTION_CUES)


def _clause_end(toks: list[_Tok], x: int) -> bool:
    """toks[x] starts a new clause, which a negation before it doesn't reach."""
    return toks[x].starts_clause or toks[x].key in _CLAUSE_WORDS


def _scope_end(toks: list[_Tok], x: int) -> bool:
    """toks[x] ends the reach of a negation before it: a new clause, but not an "or" joining a
    second thing it negates ("don't email or call")."""
    if toks[x].key in _JOINERS and not toks[x].starts_clause:
        return x + 1 < len(toks) and toks[x + 1].key in _SUBJECTS
    return _clause_end(toks, x)


def _negated(toks: list[_Tok], j: int) -> bool:
    """A negation comes before toks[j] in its clause ("we don't really deploy")."""
    if toks[j].starts_clause:  # "Don't email them. Call them.": the call is not negated
        return False
    for x in range(j - 1, -1, -1):
        if _negation(toks, x):
            return True
        if _scope_end(toks, x):
            return False
    return False


def _negated_words(raw: list[_Tok]) -> set[str]:
    """Stems of the words a negation in `raw` reaches: "don't deploy on Fridays" -> deploy, friday."""
    out: set[str] = set()
    for i in range(len(raw)):
        if not _negation(raw, i):
            continue
        for x in range(i + 1, len(raw)):
            if _scope_end(raw, x):
                break
            if raw[x].key not in _NOT_SCOPE and raw[x].key not in NEGATIONS:
                out.add(_stem(raw[x].key))
    return out


def _flips(raw: list[_Tok], out: list[_Tok]) -> list[str]:
    """A word said only under a negation that the rewrite says without one."""
    problems = []
    for w in _negated_words(raw):
        said = sum(1 for j, t in enumerate(raw) if _stem(t.key) == w and not _negated(raw, j))
        for j, t in enumerate(out):
            if _stem(t.key) == w and not _negated(out, j):
                said -= 1
                if said < 0:
                    problems.append(f"{t.surface!r} said only with a negation")
                    break
    return problems


def _forms(base: str) -> set[str]:
    forms = {base, *_IRREGULAR.get(base, "").split()}
    if len(base) >= 3:
        stem = base.removesuffix("e")
        forms |= {base + "s", base + "es", stem + "ed", stem + "ing", base + base[-1] + "ed", base + base[-1] + "ing"}
        if base.endswith("y"):
            forms |= {base[:-1] + "ies", base[:-1] + "ied"}
    return forms


# form -> {(pair index, side)}: "refuse" is the opposite of "demand" and of "accept"
_POLE: dict[str, set[tuple[int, int]]] = {}
for _n, _pair in enumerate(_OPPOSITES):
    for _side, _words in enumerate(_pair):
        for _f in (f for w in _words.split() for f in _forms(w)):
            _POLE.setdefault(_f, set()).add((_n, _side))
_PARTICLE_VERBS |= {_stem(v) for v in _PARTICLE_VERBS}  # "scaled" -> "scal", as _stem has it


def _poles(toks: list[_Tok], i: int) -> set[tuple[int, int]]:
    """The (pair, side) of toks[i] if it is one of a pair of opposites."""
    k = toks[i].key
    if k in _PARTICLES:  # only as a verb's particle, the verb up to five words back
        for x in range(i - 1, max(i - 6, -1), -1):
            if toks[x].key in _PARTICLE_VERBS or _stem(toks[x].key) in _PARTICLE_VERBS:
                return _POLE.get(k, set())
            if toks[x].starts_clause:
                break
            if toks[x].key in _PARTICLES:  # "turn the lights on and the heater off": the verb's too
                if (_POLE.get(k, set()) and {(n, 1 - side) for n, side in _POLE[k]} & _poles(toks, x)):
                    return _POLE[k]
                break
        return set()
    return _POLE.get(k, set())


def _after(toks: list[_Tok], i: int) -> tuple[str, ...]:
    """The words toks[i] is about: up to three content words after it in its clause ("approve
    the last request" -> last, request)."""
    out: list[str] = []
    for x in range(i + 1, len(toks)):
        if _clause_end(toks, x) or len(out) == 3:
            break
        if toks[x].key not in _NOT_SCOPE:
            out.append(_stem(toks[x].key))
    return tuple(out)


def _with(toks: list[_Tok], i: int, n: int, poles: list[set[tuple[int, int]]], left_only: bool = False) -> set[str]:
    """Content words in toks[i]'s clause on either side, up to another word of pair `n` ("the
    heater is on today" -> heater, today). A clause joined on by "and"/"or" without a subject
    shares what came before the pole it joins ("we need more servers and less latency": need)."""
    right: set[str] = set()
    for x in range(i + 1, len(toks)):
        if _clause_end(toks, x) or any(p[0] == n for p in poles[x]):
            break
        if toks[x].key not in _NOT_SCOPE:
            right.add(_stem(toks[x].key))
    left: set[str] = set()
    for x in range(i - 1 if not toks[i].starts_clause else -1, -1, -1):
        if any(p[0] == n for p in poles[x]):
            break
        if toks[x].key not in _NOT_SCOPE:
            left.add(_stem(toks[x].key))
        if _clause_end(toks, x):
            if not left and toks[x].key in _JOINERS and not toks[x].starts_clause:  # "and less latency"
                p = next((y for y in range(x - 1, -1, -1) if any(q[0] == n for q in poles[y])
                          or _clause_end(toks, y)), None)
                if p is not None and any(q[0] == n for q in poles[p]):
                    left = _with(toks, p, n, poles, left_only=True)
            break
    return left if left_only else left | right


def _opposites(raw: list[_Tok], out: list[_Tok]) -> list[str]:
    """A word the rewrite brings in whose opposite was said, and which itself wasn't; or, where
    both of a pair were said, one that lands on the words its opposite was said about, or the
    pair in another order than said ("reject the first, approve the last" -> "approve the first,
    reject the last"). Opposites that keep the words they were said about may move."""
    raw_poles = [_poles(raw, i) for i in range(len(raw))]
    out_poles = [_poles(out, i) for i in range(len(out))]
    said = set().union(*raw_poles)
    problems = []
    for i, t in enumerate(out):
        if any(p not in said and (p[0], 1 - p[1]) in said for p in out_poles[i]):
            problems.append(f"opposite of what was said: {t.surface}")
    for n in sorted({p[0] for p in said if (p[0], 1 - p[1]) in said}):
        raw_at = [(i, side) for i, ps in enumerate(raw_poles) for side in (0, 1) if (n, side) in ps]
        out_at = [(i, side) for i, ps in enumerate(out_poles) for side in (0, 1) if (n, side) in ps]
        about = {side: {_after(raw, i) for i, s in raw_at if s == side} - {()} for side in (0, 1)}
        moved = True  # every one of the pair keeps the words it was said about
        for i, side in out_at:
            w = _after(out, i)
            if w in about[side]:
                continue
            moved = False
            if w in about[1 - side]:
                e = next((x for x in range(i + 1, len(out)) if _clause_end(out, x)), len(out))
                problems.append(f"opposite of what was said: {_text(out[i:e])}")
        # Each one of the pair stays with the words of its clause, either side of it: never
        # with one said only with the other ("the heater is on, the fan is off" never becomes
        # "the heater is off, the fan is on"). Words said with both ("today") don't tell.
        tied = {side: set().union(*[_with(raw, i, n, raw_poles) for i, s in raw_at if s == side])
                for side in (0, 1)}
        for i, side in out_at:
            if _with(out, i, n, out_poles) & (tied[1 - side] - tied[side]):
                e = next((x for x in range(i + 1, len(out)) if _clause_end(out, x)), len(out))
                problems.append(f"opposite of what was said: {_text(out[i:e])}")
        # Otherwise the whole rewrite says the pair in the order said: its sequence is a
        # subsequence of what was said (moving whole clauses around them fails; that's cheap).
        it = iter(side for _, side in raw_at)
        if not moved and not all(side in it for _, side in out_at):
            problems.append(f"opposites out of the order said: {' '.join(out[i].surface for i, _ in out_at)}")
    return problems


_TEENS = frozenset(["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
                    "nineteen"])
_SMALL = frozenset(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]) | _TEENS
_TENS_W = frozenset(["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"])
_SCALE_W = frozenset(["hundred", "thousand", "million", "billion"])


def _split_run(keys: list[str]) -> list[tuple[int, int]]:
    """Cut a run of number words into the numbers in it: a small number or a tens word after a
    small number, or a teen after a tens word, starts a new one ("one two" = 1, 2; "twenty twenty
    six" = 20, 26; "twenty nineteen" = 20, 19)."""
    parts, start = [], 0
    for k in range(1, len(keys)):
        prev, cur = keys[k - 1], keys[k]
        if (cur in _SMALL and prev in _SMALL) or (cur in _TENS_W and (prev in _SMALL or prev in _TENS_W)) \
                or (cur in _TEENS and prev in _TENS_W):
            parts.append((start, k))
            start = k
    parts.append((start, len(keys)))
    return parts


def _pieces(keys: list[str]) -> list[tuple[int, int, int]]:
    """(start, end, value) of each number in a piece of a run: the whole piece if it reads as one
    number, else the longest stretch from the left that does, and so on ("seven and nine" = 7, 9;
    "two thousand three thousand" = 2000, 3000: a scale word stays with the number before it)."""
    out, a = [], 0
    while a < len(keys):
        if keys[a] == "and":
            a += 1
            continue
        for b in range(len(keys), a, -1):
            if b < len(keys) and keys[b] in _SCALE_W:
                continue
            if (n := words_to_number(keys[a:b])) is not None:
                out.append((a, b, n))
                a = b
                break
        else:
            a += 1
    return out


def _joined(toks: list[_Tok], nums: list[tuple[int, str]]) -> dict[int, str]:
    """Two numbers said in words back to back that read as one ("twenty twenty six" = 2026):
    index of each part -> the joined number. Digits stay as written: "1 23" is never 123."""
    out: dict[int, str] = {}
    for (i, a), (j, b) in zip(nums, nums[1:]):
        if a.isdigit() and b.isdigit() and int(a) < 100 and int(b) < 100 and j - i <= 2 \
                and all(t.key != "and" for t in toks[i + 1:j]) \
                and toks[i].key in _NUMBER_WORDS and toks[j].key in _NUMBER_WORDS:
            out[i] = out[j] = a + b.zfill(2)
    return out


def _said_numbers(toks: list[_Tok]) -> set[str]:
    """Every number said, and every pair said back to back as one."""
    nums = _numbers(toks, lone_one=True)
    return {v for _, v in nums} | set(_joined(toks, nums).values())


def _is_name(t: _Tok) -> bool:
    """A name or product: capitalised mid-sentence, or mixed case/digits ("MacBook", "i5")."""
    s = t.surface
    if t.key in _NOT_NAMES or t.key in COMMON_WORDS or not s[:1].isalpha():
        return False
    mixed = any(c.isupper() for c in s[1:]) and any(c.islower() for c in s)
    return (s[:1].isupper() and not t.starts_sentence) or mixed


def _name_key(k: str) -> str:
    return k[:-2] if k.endswith("'s") else k


def check(said: str, rewrite: str) -> list[str]:
    """Problems with `rewrite` as a rewrite of `said` (empty: it passes)."""
    raw, out = _tokens(said), _tokens(rewrite)
    if not out:
        return ["empty rewrite"]
    problems: list[str] = []
    if len(out) > len(raw) + 3:
        problems.append(f"longer than what was said ({len(out)} vs {len(raw)} words)")

    if len(raw) >= 20 and len(out) < 0.4 * len(raw):
        problems.append(f"too little left ({len(out)} of {len(raw)} words)")

    if (n_out := sum(t.key in NEGATIONS for t in out)) > (n_raw := sum(t.key in NEGATIONS for t in raw)):
        problems.append(f"more negations than were said ({n_out} vs {n_raw})")
    problems += _flips(raw, out) + _opposites(raw, out)

    raw_keys = {t.key for t in raw}
    raw_names = {_name_key(t.key) for t in raw}
    raw_nums = _said_numbers(raw)
    for _, v in _numbers(out):
        if v not in raw_nums:
            problems.append(f"number not said: {v}")
    for t in out:
        if _is_name(t) and _name_key(t.key) not in raw_names and not any(
                _name_key(t.key).startswith(r) or r.startswith(_name_key(t.key)) for r in raw_names if len(r) > 3):
            problems.append(f"name not said: {t.surface}")
        if t.key in TIME_WORDS and t.key not in raw_keys:
            problems.append(f"time word not said: {t.surface}")

    # Flips: a fact (negation, time word, number, name) gone from between words the rewrite kept
    # ("I don't want X" -> "I want X"), or a negation put between kept words. Each rewrite
    # sentence is aligned with what was said on its own, so reordering doesn't confuse it, and a
    # stretch dropped whole (a tangent, a retraction) is allowed.
    raw_keys_l = [t.key for t in raw]
    raw_num_at = dict(_numbers(raw))
    raw_joined = _joined(raw, _numbers(raw))
    kept: list[tuple[list[_Tok], dict[int, int]]] = []  # (rewrite sentence, its raw -> sentence index)
    for s, e in _sentences(out):
        sent = out[s:e]
        sk = [t.key for t in sent]
        out_of_raw: dict[int, int] = {}  # raw index -> sentence index, kept words only
        for a, b, n in difflib.SequenceMatcher(None, raw_keys_l, sk, autojunk=False).get_matching_blocks():
            if n:
                for k in range(n):
                    out_of_raw[a + k] = b + k
        sent_nums = {v for _, v in _numbers(sent)}
        for i, t in enumerate(raw):
            fact = (t.key in NEGATIONS or t.key in TIME_WORDS or i in raw_num_at or _is_name(t))
            if not fact or i in out_of_raw or (i not in raw_num_at and _name_key(t.key) in {_name_key(k) for k in sk}):
                continue  # kept, or moved elsewhere in the sentence ("today" -> "today's")
            li = next((x for x in range(i - 1, max(i - 4, -1), -1) if x in out_of_raw), None)
            ri = next((x for x in range(i + 1, min(i + 4, len(raw))) if x in out_of_raw), None)
            left = out_of_raw[li] if li is not None else None
            right = out_of_raw[ri] if ri is not None else None
            if t.key in NEGATIONS and (left is None) != (right is None) and (li == i - 1 or ri == i + 1):
                # At the edge of what was kept, right next to a kept word ("don't deploy" ->
                # "Deploy", "the answer is no" -> "The answer is"): there has to be a negation
                # near that word in the rewrite ("is it ready? no" -> "It's not ready").
                at = left if left is not None else right
                if not any(x.key in NEGATIONS for x in sent[max(at - 3, 0):at + 4]):
                    problems.append(f"dropped {t.surface!r} from: {_text(sent)}")
                continue
            if left is None or right is None or not 0 < right - left <= 2:
                continue  # not between two kept words: part of a stretch dropped whole
            if any(raw[x].key in CORRECTION_CUES for x in range(i + 1, ri)):
                continue  # "DS224, no wait, DS223" -> "DS223": taken back, not flipped
            between = {x.key for x in sent[left + 1:right]}
            if t.key in NEGATIONS and between & NEGATIONS:
                continue  # "don't" -> "do not"
            if i in raw_num_at and (raw_num_at[i] in sent_nums or raw_joined.get(i) in sent_nums):
                continue
            problems.append(f"dropped {t.surface!r} from: {_text(sent)}")
        kept.append((sent, out_of_raw))
        raw_of_out = {v: k for k, v in out_of_raw.items()}
        for j, t in enumerate(sent):
            if t.key not in NEGATIONS:
                continue
            left = next((raw_of_out[x] for x in range(j - 1, max(j - 3, -1), -1) if x in raw_of_out), None)
            right = next((raw_of_out[x] for x in range(j + 1, min(j + 3, len(sent))) if x in raw_of_out), None)
            if left is not None and right is not None and 0 < right - left <= 2 and not any(
                    raw[x].key in NEGATIONS for x in range(left, right + 1)):
                problems.append(f"added {t.surface!r} in: {_text(sent)}")
    problems += _dropped_conditions(raw, kept)
    return list(dict.fromkeys(problems))


def _condition_at(toks: list[_Tok], i: int) -> int:
    """The length of the condition (guard.CONDITIONS) starting at toks[i], or 0."""
    return max((len(p) for p in CONDITIONS if tuple(t.key for t in toks[i:i + len(p)]) == p), default=0)


def _conditions(toks: list[_Tok]) -> list[tuple[int, int]]:
    """(index, length) of each condition in `toks`, the longest at each place; one said in more
    than one word counts once ("only if" is one condition, not "only if" and "if")."""
    out, i = [], 0
    while i < len(toks):
        if n := _condition_at(toks, i):
            out.append((i, n))
            i += n
        else:
            i += 1
    return out


def _dropped_conditions(raw: list[_Tok], kept: list[tuple[list[_Tok], dict[int, int]]]) -> list[str]:
    """A condition said whose clause the rewrite keeps without one: "approve the budget if
    marketing agrees" -> "Approve the budget; marketing agrees." Each rewrite sentence that keeps
    a content word from up to four words either side of it (in its sentence) has to say a
    condition of its own, the same one or another, but an "unless" only ever pairs with an
    "unless" ("deploy unless the tests fail" is never "deploy if the tests fail"): two said
    need two ("approve it if marketing agrees and deploy if tests pass" is never "approve it
    and deploy only if tests pass"), and the one it pairs with has to say what the said one did
    ("deploy unless tests fail and email if finance approves" is never "deploy if tests fail and
    email unless finance approves"). Nothing kept there: the whole clause was dropped (a
    retraction, a tangent), which is allowed."""
    problems = []
    bounds = _sentences(raw)
    used: set[tuple[int, int]] = set()  # (rewrite sentence, index) of each condition already counted
    out_conds = [{j: sent[j].key == "unless" for j, _ in _conditions(sent)} for sent, _ in kept]
    for i, n in _conditions(raw):
        t, unless = raw[i], raw[i].key == "unless"
        lo, hi = next((s, e) for s, e in bounds if s <= i < e)
        near = [x for x in range(max(lo, i - 4), min(hi, i + n + 4))
                if not i <= x < i + n and raw[x].key not in _NOT_SCOPE and not _condition_at(raw, x)]
        holding = [k for k, (_, at) in enumerate(kept) if any(x in at for x in near)]
        if not holding:
            continue
        holding.sort(key=lambda k: -sum(x in kept[k][1] for x in near))  # the one keeping most of it first
        # What it says: the content words of its own clause ("unless [tests fail] and ..."). Where
        # the rewrite keeps any, its condition has to say them too, in its own clause.
        what = []
        for x in range(i + n, hi):
            if (x > i + n and _clause_end(raw, x)) or len(what) == 4:
                break
            if raw[x].key not in _NOT_SCOPE:
                what.append(x)
        own = next(((k, j) for k in holding for j, u in out_conds[k].items()
                    if u == unless and (k, j) not in used and _governs(kept[k], j, what)), None)
        if own is None:
            problems.append(f"dropped {t.surface!r} from: {_text(kept[holding[0]][0])}")
        else:
            used.add(own)
    return problems


def _governs(kept: tuple[list[_Tok], dict[int, int]], j: int, what: list[int]) -> bool:
    """The condition at sent[j] says what the said one did (raw indices `what`): one of those
    the rewrite keeps is in its clause, after it. Nothing of them kept: any condition will do."""
    sent, at = kept
    kept_at = [at[x] for x in what if x in at]
    if not kept_at:
        return True
    n = _condition_at(sent, j)
    end = next((x for x in range(j + n + 1, len(sent)) if _clause_end(sent, x)), len(sent))
    return any(j + n <= p < end for p in kept_at)


def _sentences(toks: list[_Tok]) -> list[tuple[int, int]]:
    starts = [i for i, t in enumerate(toks) if t.starts_sentence] or [0]
    if starts[0] != 0:
        starts.insert(0, 0)
    return list(zip(starts, [*starts[1:], len(toks)]))


def _text(toks: list[_Tok]) -> str:
    s = " ".join(t.surface for t in toks)
    return s if len(s) <= 60 else s[:57] + "..."
