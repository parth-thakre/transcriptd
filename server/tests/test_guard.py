from transcriptd.cleanup import CleanupOptions, cleanup
from transcriptd.guard import FILLERS, PROTECTED, GuardPolicy, guard, words_to_number


def _accepted(res):
    return [e for e in res.edits if e.accepted]


def _rejected(res):
    return [e for e in res.edits if not e.accepted]


def test_identity_passes_through_cleaned_casing_and_punctuation():
    res = guard("never merge a change like this", "Never merge a change like this.")
    assert res.text == "Never merge a change like this."
    assert res.edits == []


def test_negation_deletion_is_reverted():
    res = guard("never merge a change like this", "Merge a change like this.")
    assert "never" in res.text.lower()
    assert res.text.split()[0].lower() == "never"
    assert len(_rejected(res)) == 1
    assert _rejected(res)[0].reason == "protected word deleted"


def test_negation_substitution_is_reverted():
    res = guard("I did not sign it", "I did sign it.")
    assert "not" in res.text.split()
    res2 = guard("do not merge", "do now merge.")
    assert res2.text.split()[1] == "not"


def test_content_word_deletion_is_reverted():
    res = guard("deploy before friday please", "deploy friday please.")
    assert "before" in res.text
    res = guard("send the red one", "send the one.")
    assert "red" in res.text


def test_content_word_substitution_is_reverted():
    res = guard("merge it on tuesday", "merge it on thursday.")
    assert "tuesday" in res.text


def test_insertion_is_dropped():
    res = guard("ship it", "please ship it now.")
    assert res.text == "ship it"
    assert all(e.kind == "insert" and not e.accepted for e in res.edits)


def test_filler_deletion_is_accepted():
    res = guard("so um I think uh we should go", "So I think we should go.")
    assert res.text == "So I think we should go."
    assert {e.reason for e in _accepted(res)} == {"filler"}


def test_filler_deletion_can_be_disabled():
    res = guard("so um I think", "So I think.", GuardPolicy(allow_filler_deletion=False))
    assert "um" in res.text.split()


def test_stutter_collapse_accepted_but_not_for_protected():
    res = guard("I I think the the plan works", "I think the plan works.")
    assert res.text == "I think the plan works."
    res = guard("no no no do not do that", "no do not do that.")
    assert res.text.lower().split().count("no") == 3


def test_vocab_replacement_must_be_registered():
    res = guard("deploy to cooper netties", "deploy to Kubernetes.")
    assert "cooper netties" in res.text
    pol = GuardPolicy(vocab_replacements=frozenset({("cooper netties", "kubernetes")}))
    res = guard("deploy to cooper netties", "deploy to Kubernetes.", pol)
    assert res.text == "deploy to Kubernetes."


def test_hyphen_and_apostrophe_variants_are_free():
    res = guard("a well known e mail", "a well-known e-mail.")
    assert res.text == "a well-known e-mail."


def test_hindi_negation_protected():
    raw = "यह नहीं चलेगा"
    res = guard(raw, "यह चलेगा।")
    assert "नहीं" in res.text
    raw = "हे कधीच merge करू नका"
    res = guard(raw, "हे merge करू नका।")
    assert "कधीच" in res.text


def test_marathi_negation_substitution_protected():
    res = guard("आम्ही जाणार नाही", "आम्ही जाणार आहोत.")
    assert "नाही" in res.text


def test_code_switched_sentence_round_trips():
    raw = "hello everyone आम्ही इथे x प्रोजेक्टबद्दल बोलण्यासाठी आलो आहोत"
    cleaned = cleanup(raw)
    res = guard(raw, cleaned)
    assert res.text == "Hello everyone आम्ही इथे x प्रोजेक्टबद्दल बोलण्यासाठी आलो आहोत"
    assert res.edits == []


def test_devanagari_nukta_fold_is_not_a_change():
    res = guard("ज़रूर आना", "जरूर आना।")
    assert res.edits == []


def test_cleanup_only_makes_guardable_edits():
    raw = "um so I I never uh said that before"
    cleaned = cleanup(raw, CleanupOptions(ensure_terminal_punctuation=True))
    assert cleaned == "So I never said that before."
    res = guard(raw, cleaned)
    assert res.text == cleaned
    assert all(e.accepted for e in res.edits)


def test_filler_and_protected_sets_disjoint():
    assert not (FILLERS & PROTECTED)


def test_partial_replace_hiding_a_filler_and_a_real_change():
    # "um not" -> "now": the filler removal is fine but "not"->"now" must be caught.
    res = guard("I will um not go", "I will now go.")
    assert "not" in res.text.split()
    assert "now" not in res.text


def test_spoken_numbers_to_digits_is_an_allowed_edit():
    from transcriptd.guard import words_to_number

    assert words_to_number(["twenty", "five"]) == 25
    assert words_to_number(["two", "thousand", "and", "six"]) == 2006
    assert words_to_number(["one", "hundred", "fifty"]) == 150
    assert words_to_number(["not", "five"]) is None
    res = guard("ship twenty five units by nine", "Ship 25 units by 9.")
    assert res.text == "Ship 25 units by 9."
    assert {e.reason for e in res.edits} == {"number"}
    # wrong number is still a content change
    res = guard("ship twenty five units", "Ship 52 units.")
    assert "twenty five" in res.text


def test_spoken_currency_to_symbol_is_an_allowed_edit():
    res = guard("send the invoice for three hundred dollars", "Send the invoice for $300.")
    assert res.text == "Send the invoice for $300."
    res = guard("it costs two thousand rupees", "It costs ₹2000.")
    assert res.text == "It costs ₹2000."
    res = guard("about five percent", "About 5%.")
    assert res.text == "About 5%."
    # amount changed: content edit, reverted
    res = guard("three hundred dollars", "$30.")
    assert "three hundred dollars" in res.text


def test_hyphenated_spoken_numbers():
    # Whisper writes compounds with a hyphen; the key drops it ("twentyfive").
    res = guard("Do not forget the twenty-five dollars.", "Do not forget the $25.")
    assert res.text == "Do not forget the $25."
    res = guard("it costs ninety-nine", "It costs 99.")
    assert res.text == "It costs 99."
    res = guard("ship twenty-five units", "Ship 52 units.")
    assert "twenty-five" in res.text
    assert words_to_number(["twentyfive"]) == 25 and words_to_number(["twentyzero"]) is None


def test_a_number_keeps_its_decimal_point_and_sign():
    # The comparison key used to strip "." and "-": "1.5" and "15" aligned as the same word,
    # and the cleaned spelling won.
    assert guard("take 1.5 units", "Take 15 units.").text == "Take 1.5 units."
    assert guard("it was -5 degrees", "It was 5 degrees.").text == "It was -5 degrees."
    assert guard("it was −5 degrees", "It was +5 degrees.").text == "It was −5 degrees."
    assert guard("take 15 units", "Take 1.5 units.").text == "Take 15 units."
    assert guard("take 1.5 units", "Take 1.5 units.").text == "Take 1.5 units."
    assert guard("it was -5 degrees.", "It was -5 degrees.").text == "It was -5 degrees."
    # Under the allowlisted edits too: a spelling variant, a spoken number, cuts.
    assert _cut("it was -5 degrees", "It was 5 degrees.").text == "It was -5 degrees."
    assert guard("fifteen percent", "1.5%.").text == "fifteen percent."
    assert guard("fifteen dollars", "$1.5.").text == "fifteen dollars."
    assert guard("five", "-5.").text == "five."


def test_a_point_or_sign_before_a_number_stays():
    assert guard("give him .5 units", "Give him 5 units.").text == "Give him .5 units."
    assert guard("give -.5 mg", "Give 5 mg.").text == "Give -.5 mg."
    assert guard("give him .5 units", "Give him 0.5 units.").text == "Give him 0.5 units."  # the same value
    assert guard("give him .5 units", "Give him .5 units.").text == "Give him .5 units."
    assert guard("it cost -$5 today", "It cost $5 today.").text == "It cost -$5 today."
    assert guard("it cost -$5 today", "It cost -$5 today.").text == "It cost -$5 today."


def test_any_dash_or_approximate_sign_before_a_number_stays():
    for sign in "–‐‑－±~<>≈≤≥":
        assert guard("it is 5 degrees", f"It is {sign}5 degrees.").text == "It is 5 degrees.", sign
    assert guard("it is –5 degrees", "It is 5 degrees.").text == "It is –5 degrees."
    assert guard("it is ~5 degrees", "It is 5 degrees.").text == "It is ~5 degrees."
    assert guard("it is –5 degrees", "It is –5 degrees.").text == "It is –5 degrees."
    assert guard("read pages 5–10", "Read pages 5-10.").text == "Read pages 5-10."  # a range, either dash


def test_a_sign_unit_or_exponent_written_apart_is_part_of_the_number():
    assert guard("set it to 1e-3 please", "Set it to 1e3 please.").text == "Set it to 1e-3 please."
    assert guard("the change is - 5 points", "The change is 5 points.").text == "The change is - 5 points."
    assert guard("the change is - 5 points", "The change is + 5 points.").text == "The change is - 5 points."
    assert guard("growth was 5 % this year", "Growth was 5 this year.").text == "Growth was 5 % this year."
    assert guard("growth was 5 this year", "Growth was 5 % this year.").text == "Growth was 5 this year."
    assert guard("pay $ 5 now", "Pay 5 now.").text == "Pay $ 5 now."
    assert guard("it is 5 ° outside", "It is 5 outside.").text == "It is 5 ° outside."
    # The same value written together, and a dash or hyphen next to no number, are free.
    assert guard("growth was 5 % this year", "Growth was 5% this year.").text == "Growth was 5% this year."
    assert guard("the change is - 5 points", "The change is -5 points.").text == "The change is -5 points."
    assert guard("read pages 5 - 10", "Read pages 5-10.").text == "Read pages 5-10."
    assert guard("we want this - not that", "We want this, not that.").text == "We want this, not that."
    assert guard("buy:\n- 5 apples\n- 6 pears", "Buy:\n- 5 apples\n- 6 pears").text == "Buy:\n- 5 apples\n- 6 pears"


def test_a_sign_set_apart_from_a_currency_set_apart_is_part_of_the_number():
    assert guard("the balance is - $ 5 today", "The balance is $ 5 today.").text == "The balance is - $ 5 today."
    assert guard("the balance is - € 5 today", "The balance is € 5 today.").text == "The balance is - € 5 today."
    assert guard("the balance is + $ 5 today", "The balance is $ 5 today.").text == "The balance is + $ 5 today."
    assert guard("the balance is $ - 5 today", "The balance is $ 5 today.").text == "The balance is $ - 5 today."
    assert guard("the balance is - $ 5 today", "The balance is -$5 today.").text == "The balance is -$5 today."
    assert guard("the balance is $ - 5 today", "The balance is $-5 today.").text == "The balance is $-5 today."


# A sign and unit marks around a number, any order, any spacing: (said, the same value written
# together, values that drop or change a sign or unit).
MARK_VARIANTS = [
    ("- $ 5", "-$5", ["$5", "-5", "5", "+$5"]),
    ("$- 5", "-$5", ["$5", "-5", "5", "+$5"]),
    ("$ -5", "-$5", ["$5", "-5", "5"]),
    ("-$ 5", "-$5", ["$5", "-5", "5"]),
    ("$+5", "+$5", ["+5", "$5", "-$5"]),
    ("+ $5", "+$5", ["+5", "$5", "5"]),
    ("€ - 5", "-€5", ["€5", "-5", "-$5"]),
    ("5 %", "5%", ["5", "5‰"]),
    ("- 5 %", "-5%", ["5%", "-5", "5"]),
]


def test_a_sign_or_unit_any_order_any_spacing_is_part_of_the_number():
    for said, same, wrong in MARK_VARIANTS:
        assert guard(f"the change is {said} today", f"The change is {same} today.").text \
            == f"The change is {same} today.", said
        for w in wrong:
            assert guard(f"the change is {said} today", f"The change is {w} today.").text \
                == f"The change is {said} today.", (said, w)


def test_only_a_hyphen_or_en_dash_opening_a_line_is_a_bullet():
    for sign in ["−", "﹣", "－", "+", "±"]:
        said = f"{sign} $ 5 was the balance"
        assert guard(said, "$5 was the balance.").text == f"{said}.", sign
    for bullet in ["-", "–"]:
        assert guard(f"{bullet} $ 5 was the balance", "$5 was the balance.").text == "$5 was the balance."


def test_every_mark_in_a_run_counts():
    said = "Set the change to - % 5"
    for w in ["5", "-5", "%5", "+%5"]:
        assert guard(said, f"Set the change to {w}.").text == f"{said}.", w
    assert guard("The cutoff is ≥ - 5 degrees", "The cutoff is -5 degrees.").text == "The cutoff is ≥ - 5 degrees."
    assert guard("The cutoff is ≥ - 5 degrees", "The cutoff is ≥-5 degrees.").text == "The cutoff is ≥-5 degrees."


def test_pounds_are_not_a_currency_word():
    assert guard("it weighs five pounds total", "It weighs £5 total.").text == "It weighs five pounds total."
    assert guard("it weighs five pounds total", "It weighs 5 pounds total.").text == "It weighs 5 pounds total."


def test_a_zero_never_merges_with_the_number_next_to_it():
    assert guard("keep 0 23 files", "Keep 023 files.").text == "Keep 0 23 files."
    assert guard("keep 0 0 files", "Keep 00 files.").text == "Keep 0 0 files."
    assert guard("keep 10 0 files", "Keep 100 files.").text == "Keep 10 0 files."
    assert guard("keep 0 files", "Keep 00 files.").text == "Keep 0 files."
    assert words_to_number(["0", "23"]) is None and words_to_number(["0", "0"]) is None
    assert words_to_number(["10", "0"]) is None and words_to_number(["5", "twenty"]) is None
    assert words_to_number(["2", "hundred"]) == 200 and words_to_number(["two", "thousand", "5"]) == 2005
    # A repeated number is never a stutter: "twenty twenty" is 2020.
    assert guard("keep 1 1 files", "Keep 1 files.").text == "Keep 1 1 files."
    assert guard("in twenty twenty we launched", "In twenty we launched.").text == "In twenty twenty we launched."


def test_digits_written_are_the_digits_said_and_number_words_never_add_up():
    assert guard("use code 007", "Use code 7.").text == "Use code 007."  # a leading zero is a digit
    assert guard("in twenty twenty we launched", "In 40 we launched.").text == "In twenty twenty we launched."
    assert guard("press one one", "Press 2.").text == "Press one one."
    assert guard("the code is zero zero seven", "The code is 7.").text == "The code is zero zero seven."
    assert words_to_number(["one", "one"]) is None and words_to_number(["twenty", "twenty"]) is None
    assert words_to_number(["zero", "zero", "seven"]) is None and words_to_number(["007"]) is None
    assert words_to_number(["five", "twenty"]) is None and words_to_number(["twenty", "ten"]) is None
    assert words_to_number(["two", "thousand", "three", "thousand"]) is None
    # A number said as one still turns into digits.
    assert words_to_number(["two", "hundred", "five"]) == 205 and words_to_number(["nineteen", "hundred"]) == 1900
    assert words_to_number(["two", "hundred", "thousand"]) == 200_000 and words_to_number(["zero"]) == 0
    assert guard("at nine oh five pm", "At 9:05 pm.").text == "At 9:05 pm."
    assert guard("at 9 05", "At 9:05.").text == "At 9:05."


def test_numbers_side_by_side_keep_their_boundaries():
    assert guard("keep 1 23 files", "keep 12 3 files").text == "keep 1 23 files"
    assert guard("keep 1 23 files", "Keep 123 files.").text == "Keep 1 23 files."
    assert guard("keep twenty three files", "Keep 23 files.").text == "Keep 23 files."
    assert guard("at five thirty", "At 5:30.").text == "At 5:30."
    assert guard("it is COVID 19", "It is COVID-19.").text == "It is COVID-19."
    assert words_to_number(["1", "23"]) is None and words_to_number(["twenty", "5"]) is None
    assert words_to_number(["2", "thousand"]) == 2000


def test_a_number_never_changes_its_value_grouping_unit_or_form():
    assert guard("it costs 1,500 rupees", "It costs 1500 rupees.").text == "It costs 1500 rupees."  # grouping
    assert guard("it costs 1,50,000 rupees", "It costs 150000 rupees.").text == "It costs 150000 rupees."
    assert guard("it costs 1.500 rupees", "It costs 1500 rupees.").text == "It costs 1.500 rupees."
    assert guard("it costs 1,500 rupees", "It costs 1.500 rupees.").text == "It costs 1,500 rupees."
    assert guard("a 1,5 ratio", "A 15 ratio.").text == "A 1,5 ratio."
    assert guard("about 5% more", "About 5 more.").text == "About 5% more."
    assert guard("pay $5 now", "Pay 5 now.").text == "Pay $5 now."
    assert guard("pay $5 now", "Pay €5 now.").text == "Pay $5 now."
    assert guard("meet at 3:30", "Meet at 330.").text == "Meet at 3:30."
    assert guard("read pages 5-10", "Read pages 510.").text == "Read pages 5-10."
    assert guard("a 1/2 share", "A 12 share.").text == "A 1/2 share."
    assert guard("five dollars", "Rs5.").text == "Five dollars."  # rupees only for "rupees"
    assert guard("five rupees", "Rs5.").text == "Rs5."
    assert guard("twenty five", "25.").text == "25."  # a lone number is a word, not a list marker


# -- layout: line breaks and bullets ride along, words never do ---------------------------


def test_paragraph_breaks_survive():
    res = guard("first we ship on monday then we test on tuesday",
                "First, we ship on Monday.\n\n\nThen we test on Tuesday.")
    assert res.text == "First, we ship on Monday.\n\nThen we test on Tuesday."
    assert res.edits == []


def test_bullets_survive_and_joining_and_may_go():
    res = guard("buy milk eggs and bread", "Buy:\n• milk\n- eggs\n* bread")
    assert res.text == "Buy:\n• milk\n- eggs\n• bread"
    assert [(e.raw, e.reason, e.accepted) for e in res.edits] == [("and", "list joiner", True)]


def test_joiner_only_before_a_list_item():
    res = guard("buy milk and bread", "Buy milk bread.")
    assert res.text.split()[2] == "and"
    assert _rejected(res)[0].reason == "content word deleted"


def test_or_is_never_a_joiner():
    res = guard("bring milk or bread", "Bring:\n• milk\n• bread")
    assert "or" in res.text.split()


def test_layout_cannot_smuggle_words():
    res = guard("send it", "• send\n• it now")
    assert res.text == "• send\n• it"
    assert _rejected(res)[0].reason == "word inserted"


def test_reverted_span_keeps_its_line_break():
    res = guard("we will not ship it", "We will\nnever ship it.")
    assert res.text == "We will\nnot ship it."


# -- layout styles (deterministic; everything they produce must pass the guard untouched) ----


def _styled(raw, layout):
    cleaned = cleanup(raw, CleanupOptions(ensure_terminal_punctuation=True, layout=layout))
    res = guard(raw, cleaned)
    assert not _rejected(res), _rejected(res)
    assert res.text == cleaned
    return res.text


def test_note_turns_a_spoken_list_into_bullets():
    assert _styled("um I need to buy milk, eggs and bread, and then call the dentist before Friday", "note") == (
        "I need to buy:\n• milk\n• eggs\n• bread\n\nAnd then call the dentist before Friday.")
    assert _styled("We need eggs, flour, sugar, and butter.", "note") == "We need:\n• eggs\n• flour\n• sugar\n• butter"
    # A multiword first item stays together when a lead-in comes before it...
    assert _styled("Buy oat milk, eggs and bread.", "note") == "Buy:\n• oat milk\n• eggs\n• bread"
    assert _styled("I need to buy paper towels, soap and rice.", "note") == "I need to buy:\n• paper towels\n• soap\n• rice"
    # ...and an unknown verb isn't swallowed into it.
    assert _styled("Remember to grab milk, eggs and bread.", "note") == "Remember to grab:\n• milk\n• eggs\n• bread"
    assert _styled("We should fetch milk, eggs and bread.", "note") == "We should fetch:\n• milk\n• eggs\n• bread"


def test_note_leaves_clauses_alone():
    for raw in ["I went home, ate dinner and slept.", "Call Raj, it's urgent and important.",
                "I think so, but I'm not sure.", "Bring milk, eggs or bread."]:
        assert _styled(raw, "note") == raw


def test_note_breaks_paragraphs_at_topic_changes():
    assert _styled("Today was fine. Also, the meeting moved to Monday.", "note") == (
        "Today was fine.\n\nAlso, the meeting moved to Monday.")


def test_email_puts_greeting_and_signoff_on_their_own_lines():
    assert _styled("Hi John, I wanted to check if the report is ready. Thanks, Sam.", "email") == (
        "Hi John,\n\nI wanted to check if the report is ready.\n\nThanks,\nSam")
    assert _styled("Hey, the build is green. Thank you for your help.", "email") == (
        "Hey,\n\nThe build is green. Thank you for your help.")


def test_putting_back_a_deleted_opening_word_undoes_the_capital_it_caused():
    res = guard("Hey, can you send me the report by Friday?", "Can you send me the report by Friday?")
    assert res.text == "Hey, can you send me the report by Friday?"
    # a reverted span that ends a sentence keeps the next one's capital; proper nouns mid-sentence too
    assert guard("Thanks. can you call", "Can you call.").text == "Thanks. Can you call."
    assert guard("so we met london people", "We met London people.").text == "so we met London people."


# -- ramble cuts (allow_cuts): delete-only, each cut verified ------------------------------

CUTS = GuardPolicy(allow_cuts=True)


def _cut(raw, cleaned):
    return guard(raw, cleaned, CUTS)


def test_cuts_are_off_unless_asked_for():
    assert guard("So, like, I was thinking we go.", "I was thinking we go.").text == "So, like, I was thinking we go."


def test_verbal_tics_go_when_they_open_a_sentence_or_are_set_off():
    res = _cut("Okay so um I need you to, like, look at it. So basically the thing is, you know, done.",
               "I need you to look at it. The thing is done.")
    assert res.text == "I need you to look at it. The thing is done."
    assert {e.reason for e in res.edits} == {"verbal tic", "verbal tic + filler"}
    assert _cut("the socket, I mean the websocket, leaks", "the socket, the websocket, leaks").text == "the socket, the websocket, leaks"


def test_so_and_like_go_anywhere_unless_they_mean_something():
    assert _cut("give me like a song list", "give me a song list").text == "give me a song list"
    assert _cut("here we are I guess so I am 22", "here we are I guess I am 22").text == "here we are I guess I am 22"
    assert _cut("And so, my fellow Americans", "My fellow Americans").text == "My fellow Americans"
    assert _cut("I like it a lot.", "I it a lot.").text == "I like it a lot."
    assert _cut("it looks like a bug", "it looks a bug").text == "it looks like a bug"
    assert _cut("something like that", "something that").text == "something like that"
    assert _cut("I think so.", "I think.").text == "I think so."
    assert _cut("make it so that it works", "make it that it works").text == "make it so that it works"
    assert _cut("it takes like two seconds", "it takes 2 seconds").text == "it takes 2 seconds"
    assert _cut("pi, so on and so forth", "pi, on and forth").text == "pi, so on and so forth"
    # "so" joining two clauses is "therefore"; cutting it would run them together.
    said = "I live in Millbrook so I want a plan"
    assert _cut(said, "I live in Millbrook I want a plan").text == said
    assert _cut("a long ramble so buckle up", "a long ramble buckle up").text == "a long ramble so buckle up"
    assert _cut("or what, so can you add logs", "or what, can you add logs").text == "or what, can you add logs"


def test_false_starts_and_repeats_go():
    assert _cut("I'm late so start reading start the meeting", "I'm late, so start the meeting.").text == "I'm late, so start the meeting."
    assert _cut("I was gonna, I want you to refactor it", "I want you to refactor it.").text == "I want you to refactor it."
    assert _cut("we we're not closing it", "we're not closing it").text == "we're not closing it"
    assert _cut("can you, can you add logs", "can you add logs").text == "can you add logs"
    res = _cut("I want a plan that would... I live in Millbrook so I want a plan that would get me home.",
               "I live in Millbrook, so I want a plan that would get me home.")
    assert res.text == "I live in Millbrook, so I want a plan that would get me home."


def test_a_restart_needs_the_same_opening_not_just_shared_words():
    assert _cut("I told him I would come", "I would come.").text == "I told him I would come."
    assert _cut("here we are I guess so I am", "here we are guess so I am").text == "here we are I guess so I am"


def test_self_corrections_drop_the_corrected_words_numbers_included():
    assert _cut("Meet at 7, actually 3 tomorrow.", "Meet at 3 tomorrow.").text == "Meet at 3 tomorrow."
    assert _cut("Send it to John, sorry, Mary.", "Send it to Mary.").text == "Send it to Mary."
    # The cue has to be set off: "I actually did it" is not a correction.
    assert _cut("I actually did it", "did it").text == "I actually did it"


def test_a_trailed_off_half_sentence_goes_but_only_from_its_start():
    res = _cut("So what I need you to set up is... Well, actually no, I don't want you to set it up.",
               "No, I don't want you to set it up.")
    assert res.text == "No, I don't want you to set it up."
    assert _cut("I am a 22 year old... what am I saying?", "What am I saying?").text == "I am a 22 year old... what am I saying?"


def test_a_half_sentence_taken_back_goes_with_its_no():
    said = ("So what I need you to set up is... Well, actually no, I don't want you to set up. "
            "I want you to do some research for me.")
    point = "I want you to do some research for me."
    assert _cut(said, point).text == point  # the "no" and the negation take back only what's cut
    assert _cut(said, "I don't want you to set up. " + point).text == "I don't want you to set up. " + point
    assert _cut("Send it to John... no, send it to Mary.", "Send it to Mary.").text == "Send it to Mary."


def test_a_no_that_says_more_than_the_half_sentence_stays():
    for said, cleaned in [
        ("Send the invoice... no, I don't want to send it yet. Call Priya.", "Call Priya."),  # "yet"
        ("Can you call mom... no, I'll do it.", "I'll do it."),  # what was taken back isn't said again
        ("Can you call mom... no, don't call her. Call dad.", "Call dad."),  # "her" is more than was said
        ("Meet me Tuesday, no, Wednesday.", "Meet me Wednesday."),  # not left hanging: a correction of a time
        ("Deploy it to staging... no, never deploy it. Ship it.", "Ship it."),  # "never" was not said before
        ("Call mom... no.", ""),  # nothing said after it
    ]:
        assert _cut(said, cleaned).text == said, said


def test_a_trailed_off_half_sentence_may_come_out_of_a_longer_cut():
    # The LLM cut a fact along with it: the fact goes back, the half-sentence still goes.
    said = "I want a plan that would... I live in Millbrook so I want a plan that would get me home."
    res = _cut(said, "I want a plan that would get me home.")
    assert res.text == "I live in Millbrook so I want a plan that would get me home."


def test_a_correction_is_never_carved_out_of_a_paraphrase():
    said = ("So what I need you to set up is... Well, actually no, I don't want you to set up. "
            "I want you to do some research for me.")
    res = _cut(said, "I need you to do some research for me.")  # "I need" lined up with the half
    assert "set no" not in res.text and "you to set up is..." in res.text


def test_cuts_never_take_a_negation_time_word_condition_or_number():
    assert "never" in _cut("never merge a change like this", "merge a change like this.").text.lower()
    assert _cut("Meet me Tuesday, no, Wednesday.", "Meet me Wednesday.").text == "Meet me Tuesday, no, Wednesday."
    assert _cut("We should meet before... I think Friday.", "I think Friday.").text == "We should meet before... I think Friday."
    assert _cut("Do not deploy. Deploy it now.", "Deploy it now.").text == "Do not deploy. Deploy it now."
    assert _cut("only if the tests pass, you know, merge", "if the tests pass merge").text.startswith("only if")
    assert _cut("Never, actually always deploy.", "Always deploy.").text == "Never, actually always deploy."


def test_a_repeat_may_take_a_negation_because_it_is_said_again():
    assert _cut("We will not ship it, we will not ship it.", "We will not ship it.").text == "We will not ship it."


def test_a_sentence_end_is_not_a_stutter():
    assert guard("Do not deploy. Deploy it now.", "Do not deploy it now.").text == "Do not deploy. Deploy it now."
    assert _cut("Do not deploy. Deploy it now.", "Deploy it now.").text == "Do not deploy. Deploy it now."
    assert _cut("What is the list? What is the list?", "What is the list?").text == "What is the list?"


def test_an_aside_off_the_point_goes():
    said = ("I need you to fix the login page. My coffee went cold while I was typing this. "
            "It breaks on Safari when the password has a space in it.")
    res = _cut(said, "I need you to fix the login page. It breaks on Safari when the password has a space in it.")
    assert res.text == "I need you to fix the login page. It breaks on Safari when the password has a space in it."
    assert [e.reason for e in res.edits if e.accepted] == ["aside"]
    # A clause, not words carved out of one.
    assert _cut(said, said.replace("cold ", "")).text == said


def test_an_aside_needs_whisper_s_punctuation_on_both_sides():
    # Run on, a preamble going looks just like content going.
    assert _cut("I told him I would come", "I would come.").text == "I told him I would come."
    said = "I told him so I guess it was fine and then I left"
    assert _cut(said, "I left.").text == "I told him so I guess it was fine and then I left."


def test_thinking_aloud_goes_even_run_on():
    said = "This is going to be a long ramble so buckle up but here we are I guess I am stuck on the login page"
    res = _cut(said, "I am stuck on the login page.")
    assert res.text == "I am stuck on the login page."
    assert {e.reason for e in res.edits if e.accepted} == {"thinking aloud"}
    said = "I am a 22 year old... what am I saying? I don't even understand what I am saying. I study physics."
    assert _cut(said, "I am a 22 year old... I study physics.").text == "I am a 22 year old... I study physics."


def test_thinking_aloud_is_only_the_set_phrases():
    for said, cleaned in [
        ("Let me see it before you send it.", "Before you send it."),  # an object follows: content
        ("Here we are at the airport, call me.", "Call me."),
        ("Here we go.", ""),  # the whole message
        ("Hey, here we are.", "Hey."),  # a lead-in needs something after it
        ("I don't know what to say. Call me.", "Call me."),  # not one of the phrases
    ]:
        assert _cut(said, cleaned).text == said, said


def test_one_in_an_aside_is_a_pronoun_not_a_number():
    point = "Look at the login page. It breaks on Safari when the password has a space in it."
    said = point.replace("page. ", "page. This is going to be a long one, so bear with me. ")
    assert _cut(said, point).text == point
    said = point.replace("page. ", "page. It broke one time, which is odd. ")
    assert _cut(said, point).text == said


def test_an_aside_never_takes_the_point_with_it():
    tail = " It breaks on Safari when the password has a space in it."
    for aside in ["I live in Ashgrove, which is far.", "You should look at the logs first.",
                  "Can you check the session code too.", "I want you to keep the old flow.",
                  "It broke twice last week.", "It used to work before the update.", "Please keep it simple."]:
        said = "Fix the login page. " + aside + tail
        assert _cut(said, "Fix the login page." + tail).text == said, aside


def test_asides_never_take_more_than_half():
    assert _cut("This is a long ramble. Here we are.", "Here we are.").text == "This is a long ramble. Here we are."


def test_only_the_verifiable_part_of_a_cut_goes():
    # The LLM cut "Okay so" and the request; only the tic goes, and the request gets its capital.
    assert _cut("Okay so can you add logging.", "Add logging.").text == "Can you add logging."


def test_putting_words_back_undoes_the_full_stop_the_llm_put_before_them():
    res = _cut("I'm going tomorrow I think so we need a list", "I'm going tomorrow. We need a list.")
    assert res.text == "I'm going tomorrow I think so we need a list."


def test_putting_words_back_keeps_their_comma_and_drops_their_stutter():
    res = _cut("but I want, what I want is for it to show up", "but I want it to show up")
    assert res.text == "but I want, what I want is for it to show up"
    res = _cut("The bubble thing, it's, it's kind of working", "The bubble thing is kind of working.")
    assert res.text == "The bubble thing, it's kind of working."


def test_spoken_number_behind_a_filler_still_becomes_digits():
    assert _cut("retries at least, uh, three times", "retries at least 3 times").text == "retries at least 3 times"


def test_an_ellipsis_survives_reflow():
    assert guard("set up is... well", "Set up is... well").text == "Set up is... well"


def test_a_word_repeated_after_trailing_off_is_a_stutter():
    assert guard("from the place to... to a restaurant", "from the place to a restaurant").text == "from the place to a restaurant"


def test_a_cut_never_takes_a_whole_reply_or_a_false_correction():
    assert _cut("Okay.", "").text == "Okay."
    assert _cut("Send it, wait for the build.", "Send for the build.").text == "Send it, wait for the build."
    assert _cut("I'm sorry, I can't come.", "I can't come.").text == "I'm sorry, I can't come."
    assert _cut("Transfer 500, actually.", "Transfer.").text == "Transfer 500, actually."


def test_a_pause_written_as_an_ellipsis_is_not_an_abandoned_half_sentence():
    assert _cut("We need to ship by Friday... and then deploy.", "And then deploy.").text == \
        "We need to ship by Friday... and then deploy."


# -- lists and small rewrites from the LLM (with cuts) -------------------------------------


def test_a_list_drops_the_words_that_joined_its_items():
    said = "so the steps are you open the app then you go to settings and then you turn on sync"
    out = "The steps are:\n1. Open the app.\n2. Go to settings.\n3. Turn on sync."
    assert _cut(said, out).text == out
    said = "I need you to do three things first fix the login page second add logging and third update the readme"
    out = "I need you to do three things:\n- Fix the login page.\n- Add logging.\n- Update the readme."
    assert _cut(said, out).text == out
    said = "the bubble doesn't show, the pulse is slow. Oh and the the telemetry doesnt send"
    out = "Bugs:\n- The bubble doesn't show.\n- The pulse is slow.\n- The telemetry doesn't send."
    assert _cut(said, out.replace("Bugs:\n", "")).text == out.replace("Bugs:\n", "")


def test_a_numbered_item_only_drops_its_own_number():
    said = "first fix the login page third add logging"
    assert "third" in _cut(said, "1. Fix the login page.\n2. Add logging.").text
    assert _cut("one fix the page two add logs", "1. Fix the page.\n2. Add logs.").text == "1. Fix the page.\n2. Add logs."


def test_a_list_never_drops_a_negation_or_content_at_an_item():
    res = _cut("pack the charger and don't pack the laptop and the hat",
               "Pack:\n- the charger\n- pack the laptop\n- the hat")
    assert "don't" in res.text


def test_half_a_list_falls_back_to_sentences():
    said = "the steps are you open the app then you quickly go to settings then you turn on sync"
    res = _cut(said, "The steps are:\n- Open the app.\n- Go to settings.\n- Turn on sync.")
    assert "\n" not in res.text and "quickly" in res.text
    assert res.text.startswith("The steps are:")


def test_contractions_and_spoken_forms_get_written_properly():
    said = "I dont wanna change the server, we gonna ship the app and see if it get better, if it dont we look at the model"
    out = ("I don't want to change the server. We're going to ship the app and see if it gets better. "
           "If it doesn't, we look at the model.")
    res = _cut(said, out)
    assert res.text == out, _rejected(res)


def test_small_rewrites_never_change_meaning():
    assert _cut("we fix the login page", "We fixed the login page.").text == "We fix the login page."  # tense
    assert _cut("delete the file", "Delete the files.").text == "Delete the file."  # number
    assert _cut("I won't merge it", "I wont merge it.").text == "I won't merge it."  # apostrophe out of a negation
    assert _cut("I dont merge it", "I do merge it.").text == "I dont merge it."
    assert _cut("I wanna merge it", "I want merge it.").text == "I wanna merge it."
    assert _cut("we gonna ship it", "We were going to ship it.").text == "We gonna ship it."


def test_a_spoken_time_becomes_a_clock():
    assert _cut("once at like 5 30", "once at 5:30").text == "once at 5:30"
    assert _cut("meet at five thirty pm", "meet at 5:30 pm").text == "meet at 5:30 pm"
    assert _cut("meet at 5 30", "meet at 5:13").text == "meet at 5 30"


def test_a_restart_with_a_pronoun_for_the_thing_named():
    said = "the bubble should, it should be smaller"
    assert _cut(said, "The bubble should be smaller.").text == "The bubble should be smaller."
    assert _cut("we won't, they won't ship it", "We won't ship it.").text == "We won't, they won't ship it."


def test_spoken_numbers_in_a_row_become_digits_each():
    assert _cut("Testing, one, two, three mic check.", "Testing, 1, 2, 3. Mic check.").text == "Testing, 1, 2, 3. Mic check."
    assert "one, two, three" in _cut("Testing, one, two, three mic check.", "Testing, 1, 2, 4. Mic check.").text


def test_words_put_back_with_the_cleaned_full_stop_keep_the_next_capital():
    res = _cut("we tried one, two, three people said no", "We tried 1, 2, 4. People said no.")
    assert res.text == "We tried one, two, three. People said no."


def test_a_rewrite_next_to_a_cut_passes_in_parts():
    said = "I am a 30 year old... what am I saying? I work in sales."
    res = _cut(said, "I am a 30-year-old. I work in sales.")
    assert res.text == "I am a 30-year-old. I work in sales."
    assert [(e.kind, e.reason) for e in res.edits] == [("replace", "spelling variant"), ("delete", "thinking aloud")]
    # Each part still has to pass: a changed number next to the cut is put back.
    assert "30 year old" in _cut(said, "I am a 31-year-old. I work in sales.").text


def test_a_restart_never_starts_from_a_tic():
    said = "back when we were like kids something like the fair came to town"
    # ("like" before "kids" is a tic of its own; "kids something" is no restart)
    assert _cut(said, "back when we were like the fair came to town").text == \
        "back when we were kids something like the fair came to town"
    assert _cut("I'm late so start reading start the meeting", "I'm late, so start the meeting.").text == \
        "I'm late, so start the meeting."


def test_an_aside_never_takes_the_start_of_the_next_sentence():
    said = "Is the build done? My coffee is cold now. Also, the deploy is at noon."
    assert _cut(said, "Is the build done? The deploy is at noon.").text == said
    assert _cut(said, "Is the build done? Also, the deploy is at noon.").text == "Is the build done? Also, the deploy is at noon."


def test_a_list_needs_an_intro_that_is_not_half_a_sentence():
    said = "I have done a lot this year like going from the march the strike and how the courts are slow"
    res = _cut(said, "I have done a lot this year. Going from:\n- The march.\n- The strike.\n- How the courts are slow.")
    assert "\n" not in res.text and ":" not in res.text
    assert res.text == "I have done a lot this year. Going from the march, the strike, and how the courts are slow."
    # A lone number is no intro either, and one item is no list.
    assert "\n" not in _cut("one fix the page and add logs and ship it",
                            "One:\n- Fix the page.\n- Add logs.\n- Ship it.").text
    assert _cut("fix the page", "- Fix the page.").text == "Fix the page."
    # A whole intro keeps its list.
    out = "I need you to do three things:\n- Fix the login page.\n- Add logging.\n- Update the readme."
    assert _cut("I need you to do three things fix the login page add logging and update the readme", out).text == out


def test_a_vocab_rule_that_changes_part_of_its_phrase_is_accepted():
    # "1970 XT" -> "9070 XT": the diff sees only "1970" -> "9070"; it's still the learned rule,
    # but only where the rest of the rule's phrase is there too.
    pol = GuardPolicy(vocab_replacements=frozenset({("1970 xt", "9070 xt"), ("local ln", "local llm")}))
    g = guard("my 1970 XT PC is my local LN server", "My 9070 XT PC is my local LLM server.", pol)
    assert g.text == "My 9070 XT PC is my local LLM server."
    assert guard("I bought 1970 chairs", "I bought 9070 chairs.", pol).text == "I bought 1970 chairs."


def test_and_joins_number_words_only_after_hundred_or_a_scale():
    assert guard("the scores were twenty and five", "The scores were 25.").text == "The scores were twenty and five."
    assert words_to_number(["twenty", "and", "five"]) is None and words_to_number(["and", "five"]) is None
    assert words_to_number(["two", "hundred", "and", "five"]) == 205


def test_an_explicit_zero_is_never_an_omitted_multiplier():
    assert guard("the budget is 0 million dollars", "The budget is $1000000.").text == "The budget is 0 million dollars."
    assert words_to_number(["0", "million"]) is None and words_to_number(["0", "hundred"]) is None
    assert words_to_number(["two", "thousand", "hundred"]) is None
    assert words_to_number(["two", "million", "thousand"]) is None
    assert words_to_number(["million"]) == 1_000_000 and words_to_number(["a", "million"]) == 1_000_000
