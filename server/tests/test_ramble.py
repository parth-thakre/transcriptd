"""Ramble style: a free rewrite, checked for invented and flipped facts (ramble.check)."""

from tests.test_guard import MARK_VARIANTS
from tests.test_pipeline import _finish, pipeline  # noqa: F401  (fixture)
from transcriptd.context import DictationContext
from transcriptd.ramble import check

SAID = ("so um I have been thinking about the trip like we leave on Friday and I don't want to drive "
        "because the car is like twelve years old and anyway the hotel is in Jaipur and it costs 4000 a night")


def test_a_rewrite_that_keeps_the_facts_passes():
    assert check(SAID, "We leave on Friday and I don't want to drive, since the car is 12 years old. "
                       "The hotel is in Jaipur and costs 4000 a night.") == []


def test_invented_facts_fail():
    assert check(SAID, "We leave on Saturday and I don't want to drive. The hotel costs 4000 a night.")
    assert any("number" in p for p in check(SAID, "We leave on Friday. The hotel costs 5000 a night."))
    assert any("name" in p for p in check(SAID, "We leave on Friday. The hotel is in Mumbai."))


def test_a_flipped_negation_or_dropped_fact_fails():
    assert any("don't" in p for p in check(SAID, "We leave on Friday and I want to drive. The hotel is in Jaipur "
                                                 "and costs 4000 a night."))
    assert check("we haven't shipped it yet", "We have shipped it.")
    assert check("I never said she was late", "I said she was late.")
    assert check("I will finish it today", "I will not finish it today.")
    assert check("the meeting is at 5 tomorrow", "The meeting is tomorrow.")


def test_dropping_a_tangent_or_a_retraction_whole_passes():
    said = ("I want to write about my home lab and how I turned the old MacBook into a server that runs all my "
            "small stuff or even M1 M2 MacBooks after the March release anyway let's not get into that but "
            "that's what I want to write about")
    assert check(said, "I want to write about my home lab and how I turned the old MacBook into a server that "
                       "runs all my small stuff.") == []
    assert check("I was gonna say I don't know, actually I do know, it's the cache", "I do know: it's the cache.") == []


def test_numbers_said_in_words():
    assert check("the bill came in twenty twenty six and it hurt", "The bill came in 2026 and it hurt.") == []
    assert check("count one two three four five six", "Count 1, 2, 3, 4, 5, 6.") == []
    assert check("this is one big reason I want to leave", "This is a big reason I want to leave.") == []


def test_a_rewrite_that_leaves_too_little_fails():
    said = "this is going to be a long ramble so buckle up but here we are I guess so I am a 22 year old and I like tea"
    assert any("too little" in p for p in check(said, "I'm 22."))


def _ramble(p, rewrite):
    p.settings.cleanup.llm = True
    p.settings.llm.base_url = "http://fake"
    calls = []

    def rewriter(text, lists=True, ramble=False):
        calls.append((ramble, lists))
        return rewrite if ramble else text

    p.rewriter = rewriter
    return calls


def test_ramble_style_types_the_checked_rewrite(pipeline):  # noqa: F811
    p, _, _ = pipeline
    calls = _ramble(p, "We leave on Friday and I don't want to drive.")
    r = _finish(p, "so um we leave on Friday and like I don't want to drive I mean drive the car", style="ramble")
    assert r.text == "We leave on Friday and I don't want to drive."
    assert r.rewrite == {"used": True} and r.edits == []
    assert calls == [(True, True)]


def test_a_failed_check_falls_back_to_the_guarded_cleanup(pipeline):  # noqa: F811
    p, _, _ = pipeline
    calls = _ramble(p, "We leave on Friday and I want to drive.")
    r = _finish(p, "we leave on Friday and I don't want to drive", style="ramble")
    assert r.rewrite["used"] is False and r.rewrite["problems"]
    assert "don't" in r.text  # the guarded cleanup of what was said
    assert calls == [(True, True), (False, True)]


def test_ramble_in_a_chat_app_is_sentences(pipeline):  # noqa: F811
    p, _, _ = pipeline
    calls = _ramble(p, "I need three things:\n- eggs\n- milk\n- bread")
    r = _finish(p, "I need three things eggs milk and bread", style="ramble",
                context=DictationContext.with_app(None, "com.whatsapp"))
    assert calls[0] == (True, False)
    assert "\n-" not in r.text and r.rewrite == {"used": True}


def test_other_styles_never_rewrite(pipeline):  # noqa: F811
    p, _, _ = pipeline
    calls = _ramble(p, "Something else entirely.")
    r = _finish(p, "we leave on Friday", style="auto")
    assert r.rewrite is None and all(not ramble for ramble, _ in calls)


def test_a_correction_takes_back_what_came_before_it():
    assert check("it's a DS224, no wait, DS223, and it's loud", "It's a DS223, and it's loud.") == []
    assert check("meet me Tuesday, sorry, Monday at 5", "Meet me Monday at 5.") == []
    # "actually" is not a correction cue: this is still a flipped negation.
    assert check("I don't actually want it", "I want it.")


def test_a_fact_moved_or_made_possessive_is_kept():
    assert check("you can even today turn old MacBooks into Linux machines",
                 "Even today's old MacBooks can be turned into Linux machines.") == []


def test_the_examples_in_the_prompt_pass_the_check():
    from transcriptd.llm import _RAMBLE_EXAMPLES

    for said, written, as_sentences in _RAMBLE_EXAMPLES:
        assert check(said, written) == []
        assert not as_sentences or check(said, as_sentences) == []


def test_a_negation_at_the_edge_of_a_sentence_still_counts():
    assert any("don't" in p for p in check("don't deploy", "Deploy."))
    assert check("so um don't deploy it on Friday", "Deploy it on Friday.")
    assert check("the answer is no", "The answer is yes.")
    assert check("the answer is no", "The answer is.")
    assert check("I want it, not the blue one", "I want it.")


def test_a_word_said_under_a_negation_never_comes_back_without_one():
    assert check("we don't deploy on Fridays", "Fridays are for deploying.")
    assert check("ship it without the tests", "Ship it with the tests.")
    assert check("nothing changed after the update", "Everything changed after the update.")
    assert check("I dont want to drive", "I want to drive.")  # ASR writes it without the apostrophe


def test_a_rewrite_never_says_more_negations_than_were_said():
    assert check("ship it Friday", "Don't ship it until Friday.")
    assert check("I can do it", "I can't do it.")


def test_a_word_never_turns_into_its_opposite():
    assert any("refuse" in p for p in check("we demand a refund", "We refuse a refund."))
    assert check("they approved the budget", "They rejected the budget.")
    assert check("please enable the flag", "Please disable the flag.")
    assert check("we need to increase the timeout", "We need to decrease the timeout.")
    assert check("it was minus five degrees", "It was 5 degrees.")
    assert check("it was -5 degrees", "It was 5 degrees.")
    assert check("take 1.5 units", "Take 15 units.")


def test_a_rewrite_that_keeps_the_polarity_passes():
    assert check("so um yeah don't merge it until the tests pass", "Don't merge it until the tests pass.") == []
    assert check("is it ready no", "It's not ready.") == []  # "no" restated as "not"
    assert check("I do not want to drive", "I don't want to drive.") == []
    assert check("we never deploy on Fridays like ever", "We never deploy on Fridays.") == []
    assert check("I don't think it works", "I don't think it works.") == []
    assert check("they demanded a refund and we refused", "They demanded a refund, and we refused.") == []
    assert check("it was minus five degrees", "It was -5 degrees.") == []
    assert check("it was -5 degrees", "It was -5 degrees.") == []
    assert check("nobody asked for it, we just did it", "Nobody asked for it; we just did it.") == []


def test_a_point_sign_or_boundary_of_a_number_counts():
    assert check("give him .5 units", "Give him 5 units.")
    assert check("give -.5 mg", "Give 5 mg.")
    assert check("it cost -$5 today", "It cost $5 today.")
    assert check("keep 1 23 files", "Keep 123 files.")
    assert check("keep 1 23 files", "Keep 12 3 files.")
    assert check("give him .5 units", "Give him 0.5 units.") == []
    assert check("keep 1 23 files", "Keep 1 23 files.") == []
    assert check("keep 0 23 files", "Keep 023 files.")


def test_any_dash_or_approximate_sign_before_a_number_counts():
    for sign in "–‐‑－±~<>≈≤≥":
        assert check("it is 5 degrees", f"It is {sign}5 degrees."), sign
    assert check("it is –5 degrees", "It is 5 degrees.")
    assert check("it is about 5 degrees", "It is ~5 degrees.")  # cheap false reject
    assert check("it is –5 degrees", "It is -5 degrees.") == []  # the same minus
    assert check("it is ~5 degrees", "It is ~5 degrees.") == []


def test_a_sign_unit_or_exponent_written_apart_counts():
    assert check("set it to 1e-3 please", "Set it to 1e3.")
    assert check("the change is - 5 points", "The change is 5 points.")
    assert check("growth was 5 % this year", "Growth was 5 this year.")
    assert check("growth was five percent this year", "Growth was 5 this year.")
    assert check("set it to 1e-3 please", "Set it to 1e-3.") == []
    assert check("the change is - 5 points", "The change is -5 points.") == []
    assert check("growth was 5 % this year", "Growth was 5% this year.") == []
    assert check("growth was five percent this year", "Growth was 5% this year.") == []
    assert check("read pages 3 - 5 today", "Read pages 3-5 today.") == []  # a range, not a sign


def test_a_sign_set_apart_from_a_currency_set_apart_counts():
    for said in ["the balance is - $ 5 today", "the balance is $ - 5 today"]:
        assert check(said, "The balance is $5 today."), said
        assert check(said, "The balance is -$5 today.") == [], said
    assert check("the balance is - € 5 today", "The balance is €5 today.")
    assert check("the balance is + $ 5 today", "The balance is $5 today.")


def test_a_sign_or_unit_any_order_any_spacing_counts():
    for said, same, wrong in MARK_VARIANTS:
        assert check(f"the change is {said} today", f"The change is {same} today.") == [], said
        for w in wrong:
            assert check(f"the change is {said} today", f"The change is {w} today."), (said, w)


def test_one_with_a_unit_is_a_number():
    assert check("it costs one dollar now", "It costs one euro now.")
    assert check("it costs one dollar now", "It costs €1 now.")
    assert check("it is one degree now", "It is one percent now.")
    assert check("it costs $ one now", "It costs one euro now.")
    assert check("it costs one dollar now", "It costs $1 now.") == []
    assert check("it costs one dollar now", "It costs one dollar now.") == []
    assert check("this is one big reason I want to leave", "This is a big reason I want to leave.") == []


def test_pounds_are_not_a_currency_word():
    assert check("it weighs 5 pounds", "It costs £5.")
    assert check("it costs £5", "It costs £5.") == []


def test_pounds_are_a_unit_of_their_own():
    said = "We owe you 5 pounds"
    for w in ["5", "£5", "5 lbs"]:  # "lbs" is a false reject: it falls back to the guarded cleanup
        assert check(said, f"We owe you {w}."), w
    assert check(said, "We owe you five pounds.") == []


def test_only_a_hyphen_or_en_dash_opening_a_line_is_a_bullet():
    for sign in ["−", "﹣", "－", "+", "±"]:
        assert check(f"{sign} $ 5 was the balance", "$5 was the balance."), sign
    for bullet in ["-", "–"]:
        assert check(f"{bullet} $ 5 was the balance", "$5 was the balance.") == [], bullet


def test_every_mark_in_a_run_counts():
    said = "The cutoff is ≥ - 5 degrees"
    for w in ["-5", "≥5", "5", "- ≥ 5"]:
        assert check(said, f"The cutoff is {w} degrees."), w
    assert check(said, "The cutoff is ≥-5 degrees.") == []
    said = "Set the change to - % 5"
    for w in ["5", "-5", "5%"]:
        assert check(said, f"Set the change to {w}."), w
    assert check(said, "Set the change to -5%.") == []


def test_a_currency_or_degree_is_part_of_the_number():
    assert check("it costs $5 now", "It costs 5 euros now.")
    assert check("it costs 5 dollars now", "It costs €5 now.")
    assert check("it costs $5 now", "It costs 5 now.")
    assert check("it is 5 ° now", "It is 5 now.")
    assert check("it is five degrees now", "It is 5 now.")
    for rewrite in ["It costs $5 now.", "It costs 5 dollars now.", "It costs $ 5 now."]:
        assert check("it costs five dollars now", rewrite) == [], rewrite
    assert check("it costs 5€ now", "It costs five euros now.") == []
    assert check("it is minus five degrees now", "It is -5° now.") == []
    assert check("I have 3 $5 bills", "I have three $5 bills.") == []  # the $ is the 5's


def test_each_condition_kept_needs_its_own():
    said = "Approve the budget if marketing agrees, and deploy if tests pass"
    assert check(said, "Approve the budget and deploy if tests pass.")
    assert check(said, "Approve the budget if marketing agrees and deploy if tests pass.") == []
    assert check(said, "Deploy if tests pass. If marketing agrees, approve the budget.") == []


def test_unless_stays_unless():
    # "if ... not" for "unless" fails: the "not" could have gone into any "if".
    assert check("deploy unless the tests fail", "Deploy if the tests don't fail.")
    assert check("deploy unless the tests fail", "Deploy if the tests fail.")
    assert check("Deploy unless tests fail. Email if finance approves.",
                 "Deploy if tests fail. Email if finance doesn't approve.")
    assert check("Deploy unless the tests fail. If it is Friday, wait.",
                 "Deploy if the tests fail. If it is not Friday, wait.")
    assert check("deploy if the tests pass", "Deploy unless the tests pass.")
    assert check("deploy unless the tests fail", "Don't deploy unless the tests fail.")
    assert check("deploy unless the tests fail", "Don't deploy if the tests fail.")
    assert check("deploy if the tests pass", "Deploy if the tests don't pass.")
    assert check("Deploy unless tests fail. Email if finance approves.",
                 "Email if finance approves. Deploy unless tests fail.") == []


def test_a_condition_pairs_with_the_one_said_about_the_same_words():
    said = "Deploy unless tests fail and email if finance approves"
    assert check(said, "Deploy if tests fail and email unless finance approves.")
    assert check(said, "If finance approves, email, and deploy unless tests fail.") == []
    assert check(said, "Unless the tests fail, deploy; email if finance signs off.") == []


def test_a_condition_said_in_two_words_counts_once():
    said = "Approve the budget if marketing agrees and deploy if tests pass"
    assert check(said, "Approve the budget and deploy only if tests pass.")
    assert check(said, "Approve the budget only if marketing agrees and deploy only if tests pass.") == []
    assert check("deploy only if tests pass", "Deploy only if tests pass.") == []


def test_a_dropped_condition_fails():
    said = "we can approve the budget if marketing agrees to cut costs"
    assert check(said, "We can approve the budget; marketing agrees to cut costs.")
    assert check(said, "We can approve the budget.")
    assert check("ship it Friday unless the tests fail", "Ship it Friday. The tests fail.")
    assert check(said, "If marketing agrees to cut costs, we can approve the budget.") == []
    assert check(said, "We can approve the budget as long as marketing cuts costs.") == []
    assert check("ship it Friday unless the tests fail", "Ship it Friday unless the tests fail.") == []
    # A clause dropped whole, condition and all, is a tangent.
    assert check("I was thinking if we go to Goa... no, forget that. We should deploy on Friday.",
                 "We should deploy on Friday.") == []


def test_opposites_keep_the_order_they_were_said_in():
    said = "Reject the first request. Approve the last request."
    assert any("order" in p for p in check(said, "Approve the first request. Reject the last request."))
    assert check(said, "Approve the first request and reject the last one.")
    assert check(said, "Approve the last request. Reject the first request.") == []  # sentences moved
    assert check(said, "Reject the first request and approve the last one.") == []
    assert check("first of all we need more servers and less latency", "We need less latency and more servers.") == []


def test_opposites_keep_their_order_across_the_whole_rewrite():
    # Clauses each in order on their own, but the pair swapped across them.
    for said, swapped in [
        ("Approve the budget. Reject the application.", "Reject the budget. Approve the application."),
        ("reject the red one, approve the blue one", "Approve the red one, reject the blue one."),
        ("reject the red one and approve the blue one", "Approve the red one and reject the blue one."),
        ("buy Apple, sell Tesla", "Sell Apple, buy Tesla."),
        ("increase Europe, decrease Asia", "Decrease Europe, increase Asia."),
        ("enable logging then disable caching", "Disable logging, then enable caching."),
    ]:
        assert any("opposite" in p for p in check(said, swapped)), swapped
        assert check(said, said[0].upper() + said[1:] + ".") == []
    # Moved clauses that keep what each was said about pass; moved any other way, they fail.
    assert check("buy Apple, sell Tesla", "Sell Tesla, buy Apple.") == []
    assert check("Reject the first request. Approve the last request.",
                 "Approve the last one. Reject the first one.")


def test_an_opposite_never_moves_onto_what_its_partner_was_said_with():
    # Each stays with the words of its clause, either side of it; words said with both ("today",
    # "it") don't tell them apart.
    for said, swapped in [
        ("The heater is on today. The fan is off today.", "The heater is off today. The fan is on today."),
        ("enable caching on staging and disable it on prod", "Enable caching on prod and disable it on staging."),
        ("allow ssh from the office and block it from everywhere else",
         "Block ssh from the office and allow it from everywhere else."),
        ("turn the lights on and the heater off", "Turn the heater on and the lights off."),
        ("the first test passed and the second failed", "The second test passed and the first failed."),
    ]:
        assert any("opposite" in p for p in check(said, swapped)), swapped
        assert check(said, said[0].upper() + said[1:].rstrip(".") + ".") == [], said
    assert check("enable caching on staging and disable it on prod",
                 "Disable it on prod and enable caching on staging.") == []  # whole clauses moved
    assert check("The heater is on today. The fan is off today.", "The fan is off today. The heater is on today.") == []
    assert check("we need more servers and less latency", "We need less latency and more servers.") == []


def test_on_off_up_down_and_except_are_opposites_as_particles():
    assert check("so turn the flag off before the release", "Turn the flag on before the release.")
    assert check("so turn the flag off before the release", "Turn off the flag before the release.") == []
    assert check("prices went up last month", "Prices went down last month.")
    assert check("turn the living room lights off", "Turn the living room lights on.")
    assert check("the lights are off", "The lights are on.")
    assert check("keep the heater off tonight", "Keep the heater on tonight.")
    assert check("keep the heater off tonight", "Keep the heater off tonight.") == []
    assert check("ship everything except the logs", "Ship everything including the logs.")
    assert check("ship everything except the logs", "Ship everything but the logs.") == []
    # as prepositions they're nobody's opposite
    assert check("put it on the table and go up the stairs", "Put it on the table and go up the stairs.") == []
    assert check("I'll be on the call, then I'll log off", "I'll be on the call, then log off.") == []


def test_a_negation_reaches_to_the_end_of_its_clause():
    said = "do not under any circumstances whatsoever share the password with the vendor"
    assert check(said, "Share the password with the vendor.")
    assert check(said, "Never share the password with the vendor.") == []
    assert check(said, "Don't share the password with the vendor under any circumstances.") == []
    # "or" and "and" carry it on to what they join...
    said = "do not email or call the client about this"
    assert check(said, "Don't email the client about this. Call them.")
    assert check(said, "Don't email or call the client about this.") == []
    assert check("do not email and call the client", "Email and call the client.")
    assert check("don't touch it or you'll break it", "Don't touch it, or you'll break it.") == []
    # ...and no further
    assert check("I don't want to go because it's raining and the car is old",
                 "It's raining and the car is old, so I don't want to go.") == []


def test_hindi_and_marathi_negations_as_asr_romanizes_them():
    assert check("mat karo deploy", "Karo deploy.")
    assert check("abhi deploy nahi karna hai", "Abhi deploy karna hai.")
    assert check("he nako karu", "He karu.")
    assert check("deploy mat karo", "Deploy mat karo.") == []
    assert check("I bought a yoga mat and it is great", "I bought a yoga mat and it's great.") == []


def test_number_words_that_are_not_one_number_each_count():
    assert check("use pins for the headers", "Use pins seven and nine for the headers.")
    assert check("use pins seven and nine for the headers", "Use pins for the headers.")
    assert check("use pins seven and nine for the headers", "Use pins 7 and 9 for the headers.") == []
    assert check("it costs two thousand three thousand", "It costs four thousand.")
    assert check("it costs two thousand three thousand", "It costs 2000 or 3000.") == []


def test_a_year_said_as_twenty_and_a_teen():
    said = "we moved to postgres in twenty nineteen and it went fine"
    assert check(said, "We moved to Postgres and it went fine.")
    assert check(said, "We moved to Postgres in twenty eighteen and it went fine.")
    assert check(said, "We moved to Postgres in 2019 and it went fine.") == []


def test_numbers_joined_by_and_never_read_as_one():
    said = "use pins seven and nine for the headers"
    assert check(said, "Use pins 7 and 9 for the headers.") == []
    assert check(said, "Use pin 709 for the headers.") != []
