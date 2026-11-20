"""Learning from edits made after a dictation is inserted (learn.py, POST /v1/dictations/{id}/edited)."""

from tests.test_api import _wav_bytes, client  # noqa: F401  (fixture)
from tests.test_pipeline import pipeline  # noqa: F401  (fixture)
from transcriptd import learn


def test_locate_finds_the_dictation_inside_the_field():
    typed = "Meet me at the station at 5. Bring the blue bag."
    field = "hey!! Meet me at the old station at 5. Bring the red bag. see you"
    assert learn.locate(typed, field) == "Meet me at the old station at 5. Bring the red bag."
    # Edited at both ends: the changed first and last words still belong to it, the user's own
    # text around it doesn't.
    assert learn.locate("Ask Priya about the invoice tonight.", "hey Ask Riya about the bill tonight! bye") \
        == "Ask Riya about the bill tonight!"
    assert learn.locate("Never merge cooper netties", "fyi Never merge Kubernetes ok?") == "Never merge Kubernetes"
    assert learn.locate("cooper netties is down", "ugh Kubernetes is down") == "Kubernetes is down"


def test_diff_splits_swaps_deletions_and_additions():
    d = learn.diff("I watched the Ganesh Kumar video honestly yesterday",
                   "I watched the Gyanesh Kumar video yesterday and loved it")
    assert d.swaps == [("Ganesh", "Gyanesh")]
    assert d.deleted == ["honestly"] and d.added == ["and", "loved", "it"]
    assert not learn.diff("Same text.", "same text").changed


def test_preferences_learn_what_they_cut_and_what_they_put_back():
    rows = [
        ("so honestly I think it works", "Honestly, I think it works.", "I think it works."),
        ("honestly the build is fine", "Honestly, the build is fine.", "The build is fine."),
        ("it is honestly done", "It is honestly done.", "It is done."),
        # The cleanup cut "basically", they put it back, twice.
        ("it is basically ready", "It is ready.", "It is basically ready."),
        ("we are basically there", "We are there.", "We are basically there."),
        # Negations are never suggested for cutting, however often they go.
        ("not now", "Not now.", "Now."), ("not yet", "Not yet.", "Yet."), ("not here", "Not here.", "Here."),
    ]
    p = learn.preferences(rows)
    assert p.cut == ["honestly"]
    assert p.keep == ["basically"]
    assert "honestly" in p.hints() and "basically" in p.hints()


def test_examples_skip_edits_that_add_facts():
    said = ("so like I want to move the standup to Thursday because Wednesday is packed and also the "
            "client call is at 3 so yeah Thursday works better I think")
    good = "I want to move the standup to Thursday: Wednesday is packed and the client call is at 3."
    invented = "I want to move the standup to Thursday at 10: Wednesday is packed and the client call is at 3."
    rows = [(said, "typed", invented), (said, "typed", good), (said, good, good)]
    assert learn.examples(rows) == [(said, good)]


def test_edited_endpoint_records_and_learns_vocab(client):  # noqa: F811
    ids = []
    for _ in range(2):
        r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"})
        ids.append(r.json()["dictation_id"])
    # The user fixes the same misheard term in the field after each insert; the field has more text.
    for did in ids:
        r = client.post(f"/v1/dictations/{did}/edited", json={
            "final_text": "fyi Never merge Kubernetes ok?", "inserted_text": "Never merge cooper netties",
            "app": "com.whatsapp", "reason": "sent"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["changed"] and j["edited_text"] == "Never merge Kubernetes"
        assert j["swaps"][0]["heard"] == "cooper netties" and j["swaps"][0]["meant"] == "Kubernetes"
    assert j["swaps"][0].get("learned")  # the second time it becomes vocabulary
    r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.json()["text"] == "Never merge Kubernetes"
    assert client.post("/v1/dictations/999/edited", json={"final_text": "x"}).status_code == 404


def test_ramble_gets_the_users_own_edits_as_examples(pipeline, monkeypatch):  # noqa: F811
    from transcriptd import llm
    from transcriptd.api.routes import store_dictation  # noqa: F401
    from transcriptd.db import Dictation, DictationEdit

    p, _, db = pipeline
    said = ("so like I want to move the standup to Thursday because Wednesday is packed and also the "
            "client call is at 3 so yeah Thursday works better I think")
    sent = "I want to move the standup to Thursday: Wednesday is packed and the client call is at 3."
    with db.session() as s:
        d = Dictation(raw_text=said, text="typed", result_json='{"style": "ramble"}')
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(DictationEdit(dictation_id=d.id, inserted_text="typed", final_text=sent, edited_text=sent, changed=True))
        s.commit()
    p.forget_learned()
    seen = {}

    def fake_rewrite(cfg, text, *a, **kw):
        seen.update(kw)
        return text

    monkeypatch.setattr(llm, "rewrite", fake_rewrite)
    p.settings.llm.base_url, p.settings.llm.model = "http://fake", "fake"
    p.rewriter("some new ramble about the release", ramble=True)
    assert seen["examples"] == [(said, sent)]
    p.rewriter("a plain cleanup", ramble=False)
    assert seen["examples"] is None  # only Ramble gets the examples


def test_edited_contract_unchanged_and_bounded_window(client):  # noqa: F811
    from sqlmodel import select

    from transcriptd.db import DictationEdit

    did = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")},
                      data={"save": "true"}).json()["dictation_id"]
    # null: the field still held exactly what was inserted
    r = client.post(f"/v1/dictations/{did}/edited", json={"final_text": None, "inserted_text": "Never merge cooper netties",
                                                          "app": "com.whatsapp", "reason": "sent"})
    assert r.status_code == 200, r.text
    assert r.json() == {"changed": False, "edited_text": "Never merge cooper netties", "swaps": [], "deleted": [], "added": []}
    assert client.post(f"/v1/dictations/{did}/edited", json={"reason": "timeout"}).json()["changed"] is False
    with client.app.state.db.session() as s:
        rows = s.exec(select(DictationEdit).where(DictationEdit.dictation_id == did)).all()
    assert [(e.final_text, e.edited_text, e.changed, e.app) for e in rows] == [
        ("", "Never merge cooper netties", False, "com.whatsapp"), ("", rows[1].inserted_text, False, rows[1].app)]
    # a window of the field: up to 4000 characters
    window = "x " * 1000 + "Never merge Kubernetes" + " y" * 989
    assert len(window) == 4000
    r = client.post(f"/v1/dictations/{did}/edited", json={"final_text": window, "inserted_text": "Never merge cooper netties"})
    assert r.status_code == 200 and r.json()["edited_text"] == "Never merge Kubernetes"
    assert client.post(f"/v1/dictations/{did}/edited", json={"final_text": window + "z"}).status_code == 422


def test_deleting_a_dictation_drops_it_from_the_learned_examples(client):  # noqa: F811
    """The pipeline caches the Ramble examples it sends the LLM: a deleted take's words must not
    stay in that cache and go out with the next Ramble request."""
    from transcriptd.db import Dictation

    st = client.app.state
    said = ("so like I want to move the standup to Thursday because Wednesday is packed and also the "
            "client call is at 3 so yeah Thursday works better I think")
    sent = "I want to move the standup to Thursday: Wednesday is packed and the client call is at 3."
    with st.db.session() as s:
        d = Dictation(raw_text=said, text="typed", result_json='{"style": "ramble"}')
        s.add(d)
        s.commit()
        s.refresh(d)
        did = d.id
    r = client.post(f"/v1/dictations/{did}/edited", json={"final_text": sent, "inserted_text": "typed"})
    assert r.status_code == 200
    assert st.pipeline.learned()[1] == [(said, sent)]  # cached now
    assert client.delete(f"/v1/dictations/{did}").status_code == 204
    assert st.pipeline.learned()[1] == []
