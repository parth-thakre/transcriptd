"""Fixture scoring: WER plus the two numbers this project actually cares about,
protected-word survival and script correctness.

A fixture dir holds pairs `<name>.wav` + `<name>.txt` (reference transcript). Optional
`<name>.lang` holds a language hint.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .audio import load_audio
from .guard import PROTECTED
from .text import normalize_key, script_of, tokenize


def wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(hyp) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
            prev = cur
    return d[len(hyp)] / max(len(ref), 1)


def protected_survival(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    """(protected words in ref that also appear in hyp, protected words in ref)."""
    from collections import Counter

    r = Counter(k for k in ref if k in PROTECTED)
    h = Counter(k for k in hyp if k in PROTECTED)
    kept = sum(min(n, h[k]) for k, n in r.items())
    return kept, sum(r.values())


def script_agreement(ref_tokens, hyp_tokens) -> float:
    """Fraction of hyp words whose script matches the majority script of the reference
    at the same relative position (rough but catches wholesale transliteration)."""
    if not hyp_tokens or not ref_tokens:
        return 0.0
    ok = 0
    for i, t in enumerate(hyp_tokens):
        j = min(int(i * len(ref_tokens) / len(hyp_tokens)), len(ref_tokens) - 1)
        if script_of(t.surface) == script_of(ref_tokens[j].surface):
            ok += 1
    return ok / len(hyp_tokens)


def run_bench(pipeline, fixtures: Path, profiles: list[str]) -> int:
    wavs = sorted(fixtures.glob("*.wav"))
    if not wavs:
        print(f"no .wav fixtures in {fixtures}", file=sys.stderr)
        return 1
    print(f"{'profile':14} {'fixture':28} {'WER%':>6} {'prot':>7} {'script':>7} {'ms':>6}")
    for prof in profiles:
        tot_err = tot_ref = kept = protected = 0
        for wav in wavs:
            ref_path = wav.with_suffix(".txt")
            if not ref_path.exists():
                continue
            lang_path = wav.with_suffix(".lang")
            language = lang_path.read_text().strip() or None if lang_path.exists() else None
            pcm, _ = load_audio(wav)
            r = pipeline.process(pcm, profile=prof, language=language)
            ref_t = tokenize(ref_path.read_text())
            hyp_t = tokenize(r.raw_text)
            ref = [t.key for t in ref_t if t.key]
            hyp = [t.key for t in hyp_t if t.key]
            w = wer(ref, hyp)
            k, p = protected_survival(ref, hyp)
            sa = script_agreement(ref_t, hyp_t)
            tot_err += w * len(ref)
            tot_ref += len(ref)
            kept += k
            protected += p
            print(f"{prof:14} {wav.stem[:28]:28} {w*100:6.1f} {k:3}/{p:<3} {sa*100:6.0f}% {r.timings_ms.get('wall', 0):6.0f}")
        if tot_ref:
            print(f"{prof:14} {'TOTAL':28} {tot_err/tot_ref*100:6.1f} {kept:3}/{protected:<3}")
    return 0
