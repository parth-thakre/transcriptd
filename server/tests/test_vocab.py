from transcriptd.asr.base import Word
from transcriptd.vocab.matcher import VocabEntry, VocabMatcher, replacement_pairs
from transcriptd.vocab.phonetic import phonetic_key, romanize


def _words(text, conf=0.5):
    return [Word(text=t, start_ms=i * 100, end_ms=i * 100 + 90, conf=conf) for i, t in enumerate(text.split())]


def test_phonetic_keys_collide_across_scripts():
    assert romanize("कुबरनेटीस").startswith("kuba")
    k_latin = phonetic_key("Kubernetes")
    k_deva = phonetic_key("कुबरनेटीस")
    assert k_latin and k_deva
    # Not required to be identical, but must be close enough for the matcher.
    from rapidfuzz import fuzz

    assert fuzz.ratio(k_latin, k_deva) >= 70


def test_exact_learned_rule_applies_regardless_of_confidence():
    m = VocabMatcher([VocabEntry.build("Kubernetes", heard_as=["cooper netties"])])
    words, reps = m.apply(_words("deploy to cooper netties today", conf=0.99))
    assert " ".join(w.text for w in words) == "deploy to Kubernetes today"
    assert reps[0].rule == "exact"
    assert ("cooper netties", "kubernetes") in replacement_pairs(reps)


def test_phonetic_match_common_word_only_when_unsure():
    m = VocabMatcher([VocabEntry.build("Parth")])
    sure, reps = m.apply(_words("ask parth about it", conf=0.99))
    assert reps == []  # already the right word: no change
    unsure, reps = m.apply(_words("ask part about it", conf=0.6))
    assert [w.text for w in unsure][1] == "Parth"
    assert reps and reps[0].rule == "phonetic"
    confident, reps = m.apply(_words("ask part about it", conf=0.99))
    assert reps == []  # "part" is a common word and the model was sure: leave it


def test_strong_match_on_uncommon_word_replaces_even_when_confident():
    m = VocabMatcher([VocabEntry.build("Hyprland")])
    words, reps = m.apply(_words("I switched to hyperland yesterday", conf=0.99))
    assert [w.text for w in words][3] == "Hyprland"
    m = VocabMatcher([VocabEntry.build("Parth")])
    words, reps = m.apply(_words("ping parthe please", conf=0.99))
    assert [w.text for w in words][1] == "Parth"


def test_a_sound_alike_never_changes_digits():
    # "i5" as a term must not turn a confident (or unscored) "i7" into it, nor "4060" into "4070".
    m = VocabMatcher([VocabEntry.build("i5"), VocabEntry.build("RTX 4070")])
    for scored, conf in ((False, 1.0), (True, 0.99), (True, 0.3)):
        words, reps = m.apply(_words("an i7 laptop and an RTX 4060", conf=conf), scored=scored)
        assert " ".join(w.text for w in words) == "an i7 laptop and an RTX 4060" and reps == []
    # A learned rule still fixes the digits it was taught.
    m = VocabMatcher([VocabEntry.build("9070 XT", heard_as=["1970 XT"])])
    words, _ = m.apply(_words("my 1970 XT PC"), scored=False)
    assert " ".join(w.text for w in words) == "my 9070 XT PC"


def test_unscored_engine_gets_strong_tier_only():
    m = VocabMatcher([VocabEntry.build("Hyprland")])
    words, reps = m.apply(_words("hyperland is great"), scored=False)
    assert words[0].text == "Hyprland"
    m = VocabMatcher([VocabEntry.build("Parth")])
    words, reps = m.apply(_words("ask part about it"), scored=False)
    assert reps == []


def test_protected_words_never_replaced():
    m = VocabMatcher([VocabEntry.build("knot", heard_as=["not"])])
    words, reps = m.apply(_words("do not merge", conf=0.1))
    assert [w.text for w in words] == ["do", "not", "merge"]
    assert reps == []


def test_trailing_punctuation_survives_replacement():
    m = VocabMatcher([VocabEntry.build("Kubernetes", heard_as=["cooper netties"])])
    words, _ = m.apply(_words("we use cooper netties."))
    assert words[-1].text == "Kubernetes."


def test_multiword_mishearing_of_uncommon_term():
    m = VocabMatcher([VocabEntry.build("Kubernetes")])
    words, reps = m.apply(_words("deploy to cooper netties", conf=0.7))
    assert reps == [] or words[2].text.startswith("Kubernetes")
