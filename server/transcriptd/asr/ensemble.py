"""ROVER ensemble: several local engines transcribe the same take, then vote word by word.

Measured with Qwen3-ASR 1.7B as pivot + Parakeet TDT v2 + Whisper large-v3 (`transcriptd bench`):
fewer errors than any of the three alone on the author's own takes and on public Indian-English phone
audio, and no take worse than all three. The rules, in order:

1. Each member's words become spoken-form keys ("2026" -> twenty twenty six, case and
   punctuation dropped), each remembering the display word it came from.
2. A member that wrote nothing abstains (an empty Parakeet must not vote every word away), the
   pivot included (the first member that wrote words is the pivot). A member that raised abstains too.
3. The members are aligned into one word transition network: the pivot's keys first, then each
   member by edit distance against the network (similar-sounding keys pair up).
4. Slots where every member says the same thing are anchors. Between anchors is a region.
   A region holding a digit, a number word or non-Latin script is taken whole from one member:
   the key sequence most members share, ties to the earliest member. Mixing "hundred and six",
   "226" and "twenty twenty six" slot by slot invents a number nobody said, and two Latin-script
   members can otherwise vote a Devanagari aside away word by word.
5. Every other region is voted slot by slot: the key most members gave (a gap is a vote for
   nothing), ties to the earliest member.
6. Display words come verbatim from the member that supplied them (its case and punctuation).

The result's words carry the supplier's times where it has real ones, else a member's with real
times in the same slot, else neighbours'. Confidence is the share of active members that voted
for the word, times the word's own confidence from each scored member that voted for it, so the
vocab matcher sees a disputed word as unsure. The members' raw texts ride along on
`Transcript.members` for tracing.

The members are owned by the registry (they may be shared with their own profiles), so
`close()` leaves them alone.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
import unicodedata
from collections import defaultdict
from functools import lru_cache

from ..text import normalize_key
from .base import Engine, TranscribeRequest, Transcript, Word

log = logging.getLogger(__name__)

# -- spoken-form keys -------------------------------------------------------------------------

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
        "twelve": "twelfth"}
FILLER = "um"
_FOLD = {"ok": "okay", "uh": FILLER, "umm": FILLER, "uhm": FILLER, "uhh": FILLER, "hmm": FILLER, "mm": FILLER,
         "erm": FILLER, "er": FILLER, "ummm": FILLER, "alright": "all right"}
_NUM = re.compile(r"^(\d+)(st|nd|rd|th|s)?$")
_ALNUM = re.compile(r"^([a-z]+)(\d+)$")
_SPLIT = re.compile(r"[\s\-–—/]+")
_SCALES = frozenset({"hundred", "thousand", "million"})
NUMBER_WORDS = frozenset(_ONES) | frozenset(_TENS) - {"_"} | _SCALES
_DIGIT = re.compile(r"\d")
_DECIMAL = re.compile(r"(?<=\d)\.(?=\d)")
_CLOCK = re.compile(r"(?<=\d):(?=\d)")


def _say(n: int) -> list[str]:
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [_ONES[n // 100], "hundred"] + (_say(n % 100) if n % 100 else [])
    if 1100 <= n < 2100 and n % 100 and not (2000 <= n < 2010):  # years: 1999, 2026
        return _say(n // 100) + _say(n % 100)
    if n < 1_000_000:
        return _say(n // 1000) + ["thousand"] + (_say(n % 1000) if n % 1000 else [])
    return [str(n)]


def _ordinal(words: list[str]) -> list[str]:
    last = words[-1]
    if last in _ORD:
        last = _ORD[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return words[:-1] + [last]


def spoken_keys(word: str) -> list[str]:
    """Comparison keys for one display word, numbers spelled out: "2026" -> twenty twenty six,
    "22nd" -> twenty second, "GPT-4" -> gpt four, "Um," -> um. Empty for punctuation."""
    out: list[str] = []
    word = word.replace("%", " percent ").replace("+", " plus ").replace("=", " equals ").replace("&", " and ")
    # normalize_key keeps what a number's value hangs on; here that is said out loud ("1.5" is
    # one point five, "3:30" three thirty) or, for a currency symbol, not said where it's written.
    word = _CLOCK.sub(" ", _DECIMAL.sub(" point ", "".join(ch for ch in word if unicodedata.category(ch) != "Sc")))
    for raw in _SPLIT.split(word):
        k = normalize_key(raw)
        if not k:
            continue
        k = k.replace(",", "")
        m2 = _ALNUM.match(k)
        if m2 and not _NUM.match(k):
            out.append(m2.group(1))
            out.extend(_say(int(m2.group(2))))
            continue
        m = _NUM.match(k)
        if m:
            w = _say(int(m.group(1)))
            if m.group(2) in ("st", "nd", "rd", "th"):
                w = _ordinal(w)
            elif m.group(2) == "s":
                w[-1] = w[-1][:-1] + "ies" if w[-1].endswith("y") else w[-1] + "s"
            out.extend(w)
            continue
        out.extend(_FOLD.get(k, k).split())
    return out


def _non_latin(s: str) -> bool:
    return any(ch.isalpha() and not ch.isascii() and not ("À" <= ch <= "ɏ") for ch in s)


class Tok:
    """One spoken-form key from member `sys` (index into the active members), display word
    `widx` of that member."""

    __slots__ = ("key", "sys", "widx", "disp", "conf", "nkeys", "word")

    def __init__(self, key: str, sys_: int, widx: int, disp: str, conf: float, nkeys: int, word: Word):
        self.key, self.sys, self.widx, self.disp, self.conf, self.nkeys, self.word = key, sys_, widx, disp, conf, nkeys, word

    def special(self) -> bool:
        """A key whose region is taken whole: digits, number words, non-Latin script."""
        return bool(_DIGIT.search(self.disp) or _non_latin(self.disp) or self.key in NUMBER_WORDS)

    def __repr__(self) -> str:
        return f"{self.sys}:{self.key}"


def tokens(sys_: int, words: list[Word]) -> list[Tok]:
    out: list[Tok] = []
    widx = 0
    for w in words:
        for d in w.text.split():
            ks = spoken_keys(d)
            for k in ks:
                out.append(Tok(k, sys_, widx, d, w.conf, len(ks), w))
            widx += 1
    return out


# -- alignment (word transition network) ------------------------------------------------------

Slot = dict[int, "Tok | None"]


@lru_cache(maxsize=65536)
def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _align(net: list[Slot], hyp: list[Tok], sys_: int, done: list[int]) -> list[Slot]:
    """Add member `sys_` to the network by edit distance: a key matches a slot at cost 0 when a
    member there has the same key, else 1 + 0.5 * (1 - similarity to the closest key); a gap
    costs 0.5 in a slot that is already empty, else 1; an insertion costs 1."""
    S, T = len(net), len(hyp)
    inf = float("inf")
    D = [[inf] * (T + 1) for _ in range(S + 1)]
    B: list[list[str | None]] = [[None] * (T + 1) for _ in range(S + 1)]
    D[0][0] = 0.0
    keys = [{t.key for t in sl.values() if t is not None} for sl in net]

    def subcost(i: int, j: int) -> float:
        k = hyp[j].key
        if k in keys[i]:
            return 0.0
        if not keys[i]:
            return 1.5
        return 1.0 + 0.5 * (1 - max(_sim(k, x) for x in keys[i]))

    for i in range(S + 1):
        for j in range(T + 1):
            if i == 0 and j == 0:
                continue
            best, bp = inf, None
            if i and j:
                c = D[i - 1][j - 1] + subcost(i - 1, j - 1)
                if c < best:
                    best, bp = c, "m"
            if i:
                c = D[i - 1][j] + (0.5 if not keys[i - 1] else 1.0)
                if c < best:
                    best, bp = c, "d"
            if j:
                c = D[i][j - 1] + 1.0
                if c < best:
                    best, bp = c, "i"
            D[i][j], B[i][j] = best, bp
    ops = []
    i, j = S, T
    while i or j:
        bp = B[i][j]
        ops.append(bp)
        if bp == "m":
            i -= 1
            j -= 1
        elif bp == "d":
            i -= 1
        else:
            j -= 1
    ops.reverse()
    new: list[Slot] = []
    i = j = 0
    for op in ops:
        if op == "m":
            sl = dict(net[i])
            sl[sys_] = hyp[j]
            new.append(sl)
            i += 1
            j += 1
        elif op == "d":
            sl = dict(net[i])
            sl[sys_] = None
            new.append(sl)
            i += 1
        else:
            sl = {s: None for s in done}
            sl[sys_] = hyp[j]
            new.append(sl)
            j += 1
    return new


def build_net(hyps: list[list[Tok]]) -> list[Slot]:
    """hyps[0] is the pivot; the others are aligned to the growing network in order."""
    net: list[Slot] = [{0: t} for t in hyps[0]]
    done = [0]
    for s in range(1, len(hyps)):
        net = _align(net, hyps[s], s, list(done))
        done.append(s)
    return net


# -- voting -----------------------------------------------------------------------------------

class Pick:
    """An output key: the token that supplies it, the tokens that voted for it, its slot."""

    __slots__ = ("tok", "voters", "slot")

    def __init__(self, tok: Tok, voters: list[Tok], slot: Slot):
        self.tok, self.voters, self.slot = tok, voters, slot


def _key(t: Tok | None) -> str | None:
    return t.key if t is not None else None


def _vote_slots(region: list[Slot], n: int) -> list[Pick]:
    """Slot by slot: most votes wins (a gap votes for nothing), ties to the earliest member."""
    out = []
    for sl in region:
        count: dict[str | None, int] = defaultdict(int)
        first: dict[str | None, int] = {}
        for s in range(n):
            k = _key(sl[s])
            count[k] += 1
            first.setdefault(k, s)
        win = min(count, key=lambda k: (-count[k], first[k]))
        if win is None:
            continue
        voters = [sl[s] for s in range(n) if _key(sl[s]) == win]
        out.append(Pick(voters[0], voters, sl))
    return out


def _vote_span(region: list[Slot], n: int) -> list[Pick]:
    """The whole region from one member: the key sequence (fillers aside) most members share,
    ties to the earliest member."""
    spans: dict[tuple[str, ...], list[tuple[int, list[tuple[Tok, Slot]]]]] = {}
    for s in range(n):
        toks = [(sl[s], sl) for sl in region if sl[s] is not None]
        key = tuple(t.key for t, _ in toks if t.key != FILLER)
        spans.setdefault(key, []).append((s, toks))
    holders = min(spans.values(), key=lambda v: (-len(v), v[0][0]))
    _, win = holders[0]
    # The other holders' tokens, paired with the winner's by position among non-filler keys.
    others = [[t for t, _ in toks if t.key != FILLER] for _, toks in holders[1:]]
    out = []
    pos = 0
    for t, sl in win:
        voters = [t]
        if t.key != FILLER:
            voters += [o[pos] for o in others]
            pos += 1
        out.append(Pick(t, voters, sl))
    return out


def vote(net: list[Slot], n: int) -> list[Pick]:
    """Anchors (all members agree) from the pivot; regions with numbers or non-Latin
    script whole from one member; other regions slot by slot."""
    anchor = [len({_key(sl[s]) for s in range(n)}) == 1 for sl in net]
    special = [any(t is not None and t.special() for t in sl.values()) for sl in net]
    # "and" after a scale word belongs to the number ("one hundred and twenty three"); "2 and 3"
    # are two numbers, each voted on its own.
    for i in range(1, len(net) - 1):
        if (special[i + 1] and any(t is not None and t.key == "and" for t in net[i].values())
                and any(t is not None and t.key in _SCALES for t in net[i - 1].values())):
            special[i] = True
    # Units: each anchor alone, each run of non-anchor slots as one region.
    units: list[tuple[int, int, bool]] = []  # (start, end, is_anchor)
    i = 0
    while i < len(net):
        e = i + 1
        if not anchor[i]:
            while e < len(net) and not anchor[e]:
                e += 1
        units.append((i, e, anchor[i]))
        i = e

    def special_region(u: tuple[int, int, bool]) -> bool:
        return not u[2] and any(special[u[0]:u[1]])

    def special_anchor(u: tuple[int, int, bool]) -> bool:
        return u[2] and special[u[0]]

    out: list[Pick] = []
    k = 0
    while k < len(units):
        u = units[k]
        if special_region(u) or (special_anchor(u) and _reaches_special_region(units, k, special_region,
                                                                             special_anchor)):
            # A number (or non-Latin) stretch with the number words everyone agreed on around it,
            # whole from one member: "123" / "224" / "233" must not become "223" by voting the
            # differing keys on either side of a shared "hundred twenty" separately.
            e = k + 1
            while e < len(units) and (special_region(units[e]) or special_anchor(units[e])):
                e += 1
            out.extend(_vote_span(net[u[0]:units[e - 1][1]], n))
            k = e
            continue
        if u[2]:
            if net[u[0]][0] is not None:
                out.append(Pick(net[u[0]][0], [net[u[0]][s] for s in range(n)], net[u[0]]))  # type: ignore[misc]
        else:
            out.extend(_vote_slots(net[u[0]:u[1]], n))
        k += 1
    return out


def _reaches_special_region(units, k, special_region, special_anchor) -> bool:
    """Whether the run of number anchors starting at unit k leads into a number region."""
    while k < len(units) and special_anchor(units[k]):
        k += 1
    return k < len(units) and special_region(units[k])


# -- the engine -------------------------------------------------------------------------------

def _time_quality(engine: Engine) -> int:
    """2: real per-word times; 1: segment times spread over words; 0: none (spread over the take)."""
    q = getattr(engine, "word_time_quality", None)
    if q is not None:
        return int(q)
    kind = getattr(getattr(engine, "caps", None), "max_timestamp_kind", None)
    return {"word": 2, "token": 2, "segment": 1}.get(kind, 0)


class EnsembleEngine:
    name = "ensemble"

    def __init__(self, members: list[tuple[str, Engine]], *, concurrent: bool = False):
        if len(members) < 2:
            raise ValueError("an ensemble needs at least two members")
        self.members = list(members)
        self.model_name = "+".join(n for n, _ in members)
        # One thread per member was measured slower on one GPU (median 487 vs 387 ms on phone
        # takes, 2.9 vs 2.0 s on a 55 s take): the runs contend for the device. Off by default.
        self.concurrent = concurrent
        # The long-take front end (asr/chunking.py): the smallest member window, and the take
        # whole minus its silent tail, so Whisper never decodes a window of silence. Every member
        # gets the trimmed take; measured, that was as good or better for Qwen3 and Parakeet than
        # the whole take (2 fewer errors over 39 takes past 30 s).
        wins = [w for _, e in members if (w := getattr(e, "window_ms", 30_000)) is not None]
        self.window_ms = min(wins) if wins else None
        self.long_form = True
        self._quality = [_time_quality(e) for _, e in members]

    def supports_streaming(self) -> bool:
        return False

    def close(self) -> None:  # members belong to the registry
        pass

    def _run_all(self, req: TranscribeRequest) -> list[Transcript | BaseException]:
        results: list[Transcript | BaseException] = [RuntimeError("not run")] * len(self.members)

        def run(i: int) -> None:
            name, eng = self.members[i]
            try:
                results[i] = eng.transcribe(req)
            except Exception as exc:  # a failing member abstains
                log.warning("ensemble member %s failed, abstains: %s", name, exc)
                results[i] = exc

        if self.concurrent:
            threads = [threading.Thread(target=run, args=(i,), name=f"asr-{n}") for i, (n, _) in enumerate(self.members)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        else:
            for i in range(len(self.members)):
                run(i)
        return results

    def transcribe(self, req: TranscribeRequest) -> Transcript:
        t0 = time.perf_counter()
        results = self._run_all(req)
        t_asr = time.perf_counter()
        ok = [(i, r) for i, r in enumerate(results) if isinstance(r, Transcript)]
        if not ok:
            raise RuntimeError(f"every ensemble member failed: {results[0]}") from results[0]  # type: ignore[misc]
        timings: dict[str, float] = {}
        for i, r in enumerate(results):
            if isinstance(r, Transcript) and "wall" in r.timings_ms:
                timings[f"asr_{self.members[i][0]}"] = r.timings_ms["wall"]
        # Active members: those that wrote words. An empty one abstains, the pivot included (an
        # empty pivot would otherwise win every 1-1 tie against the member that heard speech);
        # the first active member is then the pivot.
        toks = {i: tokens(i, r.words) for i, r in ok}
        active = [i for i, _ in ok if toks[i]]
        if not active:
            pivot = ok[0][1]
            return Transcript(words=[], language=pivot.language or req.language, scored=True, engine=self.name,
                              model=self.model_name, timings_ms=timings,
                              members={self.members[i][0]: (r.text if isinstance(r, Transcript) else f"<failed: {r}>")
                                       for i, r in enumerate(results)})
        n = len(active)
        hyps = [[Tok(t.key, a, t.widx, t.disp, t.conf, t.nkeys, t.word) for t in toks[m]] for a, m in enumerate(active)]
        picks = vote(build_net(hyps), n)
        trs = [results[m] for m in active]
        words = self._render(picks, n, trs, [self._quality[m] for m in active])  # type: ignore[arg-type]
        pivot = results[active[0]]
        assert isinstance(pivot, Transcript)
        timings["vote"] = round((time.perf_counter() - t_asr) * 1000, 1)
        timings["wall"] = round((time.perf_counter() - t0) * 1000, 1)
        return Transcript(
            words=words,
            language=pivot.language or req.language,
            scored=True,
            engine=self.name,
            model=self.model_name,
            timings_ms=timings,
            members={self.members[i][0]: (r.text if isinstance(r, Transcript) else f"<failed: {r}>")
                     for i, r in enumerate(results)},
        )

    def _render(self, picks: list[Pick], n: int, trs: list[Transcript], quality: list[int]) -> list[Word]:
        """Output words: a display word whose keys all won from one member verbatim, else its
        winning keys one by one. Times and confidence as the module docstring says."""

        def conf(p: Pick) -> float:
            c = len(p.voters) / n
            for v in p.voters:
                if trs[v.sys].scored:
                    c *= v.conf
            return c

        def times(p: Pick) -> tuple[int, int] | None:
            best = max(quality[p.tok.sys], 0)
            if best >= 2:
                return p.tok.word.start_ms, p.tok.word.end_ms
            mates = [t for t in (*p.voters, *(t for t in p.slot.values() if t is not None)) if quality[t.sys] >= 2]
            if mates:
                return mates[0].word.start_ms, mates[0].word.end_ms
            if best == 1:
                return p.tok.word.start_ms, p.tok.word.end_ms
            return None

        out: list[tuple[Word, bool]] = []  # (word, has real-ish times)
        k = 0
        while k < len(picks):
            tok = picks[k].tok
            e = k + 1
            while e < len(picks) and picks[e].tok.sys == tok.sys and picks[e].tok.widx == tok.widx:
                e += 1
            group = picks[k:e]
            if e - k == tok.nkeys:
                ts = times(group[0])
                c = min(conf(p) for p in group)
                out.append((Word(tok.disp, *(ts or (0, 0)), conf=c), ts is not None))
            else:
                for p in group:
                    ts = times(p)
                    out.append((Word(p.tok.key, *(ts or (0, 0)), conf=conf(p)), ts is not None))
            k = e
        return _fill_times(out)


def _fill_times(words: list[tuple[Word, bool]]) -> list[Word]:
    """Words without times get them spread between their timed neighbours."""
    known = [i for i, (_, ok) in enumerate(words) if ok]
    if not known:
        return [w for w, _ in words]
    res = [w for w, _ in words]
    i = 0
    while i < len(words):
        if words[i][1]:
            i += 1
            continue
        e = i
        while e < len(words) and not words[e][1]:
            e += 1
        lo = res[i - 1].end_ms if i > 0 else res[e].start_ms if e < len(res) else 0
        hi = res[e].start_ms if e < len(res) else lo
        hi = max(hi, lo)
        step = (hi - lo) / (e - i)
        for x in range(i, e):
            res[x].start_ms = int(lo + (x - i) * step)
            res[x].end_ms = int(lo + (x - i + 1) * step)
        i = e
    return res
