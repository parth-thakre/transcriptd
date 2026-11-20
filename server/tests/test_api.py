import numpy as np
import soundfile as sf
import io
import json

import pytest
from fastapi.testclient import TestClient

from transcriptd.app import create_app
from transcriptd.config import Settings
from tests.test_pipeline import FakeEngine, _w


@pytest.fixture
def client(tmp_path):
    s = Settings.load(tmp_path / "nope.toml")
    s.data_dir = tmp_path
    s.models_dir = tmp_path
    app = create_app(s)
    with TestClient(app) as c:
        app.state.registry._engines["english"] = FakeEngine(_w("never um merge cooper netties"))
        c.headers["Authorization"] = f"Bearer {s.token}"
        yield c


def _wav_bytes(seconds=1.0):
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(16000 * seconds), dtype=np.float32), 16000, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_auth_required(client):
    r = client.get("/v1/health", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_health(client):
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["ok"]


def test_transcribe_file_and_vocab_and_correction_loop(client):
    r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["text"] == "Never merge cooper netties"
    did = j["dictation_id"]

    # user fixes it in the overlay -> correction -> suggestion
    r = client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes", "dictation_id": did})
    assert r.json()["suggest"] is True

    # user accepts -> vocab term with heard_as rule
    r = client.post("/v1/vocab", json={"term": "Kubernetes", "heard_as": ["cooper netties"], "added_from": "overlay_edit"})
    assert r.status_code == 201
    r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.json()["text"] == "Never merge Kubernetes"

    # a second correction for a known term learns silently
    r = client.post("/v1/corrections", json={"heard": "cube or netties", "meant": "Kubernetes"})
    assert r.json()["suggest"] is False and r.json()["reason"] == "learned heard_as"
    terms = client.get("/v1/vocab").json()
    assert sorted(terms[0]["heard_as"]) == ["cooper netties", "cube or netties"]

    # suppression
    client.post("/v1/vocab/suppress", json={"word": "foo"})
    r = client.post("/v1/corrections", json={"heard": "fool", "meant": "foo"})
    assert r.json()["reason"] == "suppressed"

    # history
    hist = client.get("/v1/dictations").json()
    assert hist[0]["id"] == did


def test_vocab_import_export(client):
    r = client.post("/v1/vocab/import", json={"text": "Parth | parth thakre\nHyprland | | hyper land, hip land\n"})
    assert r.json()["added"] == 2
    txt = client.get("/v1/vocab/export").text
    assert "Hyprland |  | hip land, hyper land" in txt


def test_dictate_websocket(client):
    with client.websocket_connect(f"/v1/dictate?token={client.app.state.settings.token}") as ws:
        ws.send_text(json.dumps({"type": "start", "profile": "english", "app_hint": "test",
                                 "context": {"app": "test", "before": "Note:", "after": ""}}))
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(np.zeros(16000, dtype="<i2").tobytes())
        ws.send_text(json.dumps({"type": "end"}))
        j = ws.receive_json()
        assert j["type"] == "final"
        assert j["text"] == "Never merge cooper netties"
        assert j["insert_text"] == " never merge cooper netties "  # after "Note:" = mid-sentence
        assert j["dictation_id"]
        assert [e["reason"] for e in j["edits"]] == ["filler"]


def test_readback_corrections_learn_silently_then_auto_add(client):
    r = client.post("/v1/corrections", json={"heard": "hyper land", "meant": "Hyprland", "source": "readback"})
    assert r.json() == {"suggest": False, "reason": "recorded", "term": "Hyprland", "times_seen": 1}
    r = client.post("/v1/corrections", json={"heard": "hyperland", "meant": "Hyprland", "source": "readback"})
    j = r.json()
    assert j["reason"] == "auto-learned" and j["learned"] is True
    terms = {t["term"]: t for t in client.get("/v1/vocab").json()}
    assert sorted(terms["Hyprland"]["heard_as"]) == ["hyper land", "hyperland"]
    r = client.post("/v1/corrections", json={"heard": "now", "meant": "not", "source": "readback"})
    assert r.json()["reason"] == "protected word"


def test_restyle_and_delete_dictation(client):
    j = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")},
                    data={"save": "true", "style": "message", "llm": "false", "app_hint": "phone"}).json()
    assert j["style"] == "message" and j["text"] == "Never merge cooper netties."
    did = j["dictation_id"]
    assert {s["name"] for s in client.get("/v1/health").json()["styles"]} >= {"auto", "message", "note", "email"}

    r = client.post(f"/v1/dictations/{did}/restyle", json={"style": "auto", "llm": False})
    assert r.status_code == 200 and r.json()["text"] == "Never merge cooper netties" and r.json()["dictation_id"] == did
    assert client.get(f"/v1/dictations/{did}").json()["result"]["style"] == "auto"
    assert client.get("/v1/dictations").json()[0]["app_hint"] == "phone"

    client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes", "dictation_id": did})
    assert client.delete(f"/v1/dictations/{did}").status_code == 204
    assert client.get(f"/v1/dictations/{did}").status_code == 404
    assert client.post(f"/v1/dictations/{did}/restyle", json={"style": "auto"}).status_code == 404


def test_phone_app_is_served_without_a_token(client):
    client.headers.pop("Authorization")
    assert client.get("/", follow_redirects=False).headers["location"] == "/m/"
    page = client.get("/m/")
    assert page.status_code == 200 and "app.js" in page.text and page.headers["cache-control"] == "no-cache"
    assert client.get("/m/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
    assert client.get("/v1/health").status_code == 401


def test_fixing_a_common_word_is_never_learned(client):
    for _ in range(3):
        r = client.post("/v1/corrections", json={"heard": "to", "meant": "Kubernetes", "source": "phone_edit"})
        assert r.json() == {"suggest": False, "reason": "common word"}
    assert client.get("/v1/vocab").json() == []
    # ...and those fixes don't count toward learning the term later, or "to" would become an alias.
    r = client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes", "source": "phone_edit"})
    assert r.json()["times_seen"] == 1 and not r.json().get("learned")
    assert client.get("/v1/vocab").json() == []


def test_upload_fallback_takes_the_same_context_as_the_socket(client):
    ctx = json.dumps({"app": "com.google.android.apps.messaging", "before": "hello", "after": ""})
    j = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")},
                    data={"llm": "false", "context": ctx}).json()
    assert j["insert_text"].startswith(" never")  # fitted to the caret, like the socket path
    bad = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"context": "[1]"})
    assert bad.status_code == 400


def test_takes_are_kept_with_their_audio_stats_and_client_reports(client):
    client.app.state.settings.keep_dictation_audio = True
    t = np.arange(16000) / 16000
    buf = io.BytesIO()
    sf.write(buf, (0.3 * np.sin(2 * np.pi * 440 * t) * (t > 0.5)).astype(np.float32), 16000, format="WAV", subtype="PCM_16")
    j = client.post("/v1/transcribe", files={"file": ("a.wav", buf.getvalue(), "audio/wav")}, data={"save": "true"}).json()
    a = j["audio"]
    assert a["speech_db"] > -15 and a["noise_db"] < -80 and a["snr_db"] > 60
    assert 480 <= a["lead_ms"] <= 520 and a["rumble_pct"] < 1
    did = j["dictation_id"]
    wav = client.app.state.settings.data_dir / "dictations" / f"{did}.wav"
    assert wav.exists()  # the upload path keeps the take too, not only the socket

    report = {"client": "android", "dictation_id": did, "outcome": "typed in", "expected": "Never merge Kubernetes",
              "mic": {"source_name": "raw"}}
    assert client.post("/v1/telemetry", json=report).status_code == 201
    assert client.post("/v1/telemetry", json={"client": "android", "outcome": "not sent"}).status_code == 201
    assert client.post("/v1/telemetry", json=[1]).status_code == 400
    assert client.post("/v1/telemetry", content=b"{" + b" " * 70_000 + b"}").status_code == 413
    rows = client.get("/v1/telemetry").json()
    assert [r["report"]["outcome"] for r in rows] == ["not sent", "typed in"]
    assert rows[0]["dictation"] is None
    assert rows[1]["dictation"]["raw_text"] == "never um merge cooper netties" and rows[1]["dictation"]["audio"] == a

    # deleting the dictation deletes its audio, but the report stays (unlinked)
    assert client.delete(f"/v1/dictations/{did}").status_code == 204
    assert not wav.exists()
    assert client.get("/v1/telemetry").json()[1]["dictation_id"] is None


def test_deleting_a_dictation_deletes_the_field_text_kept_with_it(client):
    from sqlmodel import select

    from transcriptd.db import DictationEdit

    did = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"}).json()["dictation_id"]
    r = client.post(f"/v1/dictations/{did}/edited", json={"final_text": "private: Never merge Kubernetes", "reason": "sent"})
    assert r.status_code == 200
    db = client.app.state.db
    with db.session() as s:
        assert len(s.exec(select(DictationEdit).where(DictationEdit.dictation_id == did)).all()) == 1
    assert client.delete(f"/v1/dictations/{did}").status_code == 204
    with db.session() as s:
        assert s.exec(select(DictationEdit)).all() == []
    # ...while the correction the edit taught is kept, unlinked
    assert client.get(f"/v1/dictations/{did}").status_code == 404


def test_one_take_sending_the_same_correction_twice_counts_once(client):
    did = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"}).json()["dictation_id"]
    fix = {"heard": "hyper land", "meant": "Hyprland", "source": "readback", "dictation_id": did}
    assert client.post("/v1/corrections", json=fix).json()["times_seen"] == 1
    r = client.post("/v1/corrections", json=fix).json()  # a retry, or the field reported twice
    assert r["times_seen"] == 1 and not r.get("learned")
    r = client.post("/v1/corrections", json={**fix, "heard": "hyperland"}).json()  # another fix, same take
    assert r["times_seen"] == 1 and not r.get("learned")
    assert client.get("/v1/vocab").json() == []
    did2 = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"}).json()["dictation_id"]
    r = client.post("/v1/corrections", json={**fix, "dictation_id": did2}).json()
    assert r["reason"] == "auto-learned" and r["times_seen"] == 2


def test_restyle_of_a_dictation_deleted_meanwhile_is_a_404(client, monkeypatch):
    did = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"}).json()["dictation_id"]
    pipeline = client.app.state.pipeline
    real = pipeline.restyle

    def restyle_while_deleted(*a, **kw):
        out = real(*a, **kw)
        with client.app.state.db.session() as s:
            from transcriptd.db import Dictation
            s.delete(s.get(Dictation, did))
            s.commit()
        return out

    monkeypatch.setattr(pipeline, "restyle", restyle_while_deleted)
    assert client.post(f"/v1/dictations/{did}/restyle", json={"style": "message", "llm": False}).status_code == 404


def test_an_edit_for_a_dictation_deleted_meanwhile_is_not_kept(client, monkeypatch):
    """dictation_edited reads the dictation, works out the edit, then stores it: a delete in
    between must leave no edit (the field's text) behind, though SQLite enforces no foreign key."""
    from types import SimpleNamespace

    from sqlmodel import select

    from transcriptd import learn
    from transcriptd.api import routes
    from transcriptd.db import DictationEdit

    did = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"}).json()["dictation_id"]
    real = learn.locate

    def locate_while_deleted(typed, field):
        routes.delete_dictation(SimpleNamespace(app=client.app), did)
        return real(typed, field)

    monkeypatch.setattr(learn, "locate", locate_while_deleted)
    r = client.post(f"/v1/dictations/{did}/edited", json={"final_text": "private: Never merge Kubernetes"})
    assert r.status_code == 404
    with client.app.state.db.session() as s:
        assert s.exec(select(DictationEdit)).all() == []


def test_insert_if_exists_needs_the_parent_row(client):
    from transcriptd.db import Dictation, DictationEdit

    db = client.app.state.db
    did = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"}).json()["dictation_id"]
    new_id = db.insert_if_exists(DictationEdit(dictation_id=did, inserted_text="a", edited_text="b", changed=True),
                                 Dictation, did)
    with db.session() as s:
        row = s.get(DictationEdit, new_id)
        assert (row.dictation_id, row.inserted_text, row.edited_text, row.changed, row.user_id) == (did, "a", "b", True, 1)
        assert row.created_at is not None
    assert db.insert_if_exists(DictationEdit(dictation_id=did + 100), Dictation, did + 100) is None


def _save_take(client):
    r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")}, data={"save": "true"})
    return r.json()["dictation_id"]


def test_a_deleted_dictations_id_is_never_given_to_the_next_take(client):
    """Android posts a take's edit up to 90 s later: by then the take may be deleted and another
    one made. Its edit must find nothing, not land on the new take."""
    from sqlmodel import select

    from transcriptd.db import DictationEdit

    first, newest = _save_take(client), _save_take(client)
    assert client.delete(f"/v1/dictations/{newest}").status_code == 204
    nxt = _save_take(client)
    assert nxt > newest > first
    r = client.post(f"/v1/dictations/{newest}/edited", json={"final_text": "private: Never merge Kubernetes"})
    assert r.status_code == 404
    with client.app.state.db.session() as s:
        assert s.exec(select(DictationEdit)).all() == []


def test_a_fresh_database_gives_dictations_and_meetings_autoincrement(tmp_path):
    import sqlite3

    from transcriptd.db import Database

    path = tmp_path / "transcriptd.db"
    Database(path).engine.dispose()
    con = sqlite3.connect(path)
    try:
        ddl = dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))
    finally:
        con.close()
    assert all("AUTOINCREMENT" in ddl[t] for t in ("dictation", "meeting"))


def test_a_dictation_deleted_while_its_audio_is_written_keeps_no_audio(client, monkeypatch):
    # The row is visible before its WAV is attached: a delete in that gap must still take the
    # audio with it, not leave a file the API can no longer reach.
    from types import SimpleNamespace

    from sqlmodel import select

    from transcriptd.api import routes
    from transcriptd.db import Dictation

    st = client.app.state
    st.settings.keep_dictation_audio = True
    real_write = sf.write
    deleted = []

    def write_after_a_delete(*a, **k):
        with st.db.session() as s:
            did = s.exec(select(Dictation.id).order_by(Dictation.id.desc())).first()
        routes.delete_dictation(SimpleNamespace(app=client.app), did)
        deleted.append(did)
        return real_write(*a, **k)

    monkeypatch.setattr(sf, "write", write_after_a_delete)
    out = {"profile": "english", "language": "en", "raw_text": "never", "text": "Never", "duration_ms": 1000}
    did = routes.store_dictation(st, out, None, np.zeros(16000, dtype=np.float32))
    assert deleted == [did]
    assert not (st.settings.data_dir / "dictations" / f"{did}.wav").exists()
    assert client.get(f"/v1/dictations/{did}").status_code == 404


def test_a_wav_whose_write_fails_is_not_left_behind(client, monkeypatch):
    # The dictation is deleted just before its WAV opens, then the write fails (a full disk):
    # the half-written file must not stay with no row to reach it.
    from types import SimpleNamespace

    from sqlmodel import select

    from transcriptd.api import routes
    from transcriptd.db import Dictation

    st = client.app.state
    st.settings.keep_dictation_audio = True

    real_opener = routes.private_opener

    def delete_then_open(path, flags):
        with st.db.session() as s:
            did = s.exec(select(Dictation.id).order_by(Dictation.id.desc())).first()
        routes.delete_dictation(SimpleNamespace(app=client.app), did)
        return real_opener(path, flags)

    def fail_midway(fh, *a, **k):
        fh.write(b"RIFF" + b"\0" * 4096)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(routes, "private_opener", delete_then_open)
    monkeypatch.setattr(sf, "write", fail_midway)
    out = {"profile": "english", "language": "en", "raw_text": "never", "text": "Never", "duration_ms": 1000}
    with pytest.raises(OSError):
        routes.store_dictation(st, out, None, np.zeros(16000, dtype=np.float32))
    assert list((st.settings.data_dir / "dictations").iterdir()) == []


def _delete_vocab_after_history_read(client, monkeypatch, tid):
    """When record_correction next builds its vocab lookup (after it read the correction
    history), delete vocab term `tid` from another thread, as a second request would, and give
    it up to half a second. Returns the thread; join it after the correction."""
    import threading
    from types import SimpleNamespace

    from transcriptd.api import routes
    from transcriptd.db import VocabTerm

    real_select, started = routes.select, []

    def select_then_delete(*a, **k):
        if a and a[0] is VocabTerm and not started:
            t = threading.Thread(target=routes.delete_vocab, args=(SimpleNamespace(app=client.app), tid))
            started.append(t)
            t.start()
            t.join(0.5)  # done by now unless the correction holds the write lock
        return real_select(*a, **k)

    monkeypatch.setattr(routes, "select", select_then_delete)
    return started


def test_a_correction_racing_a_vocab_delete_does_not_bring_the_term_back(client, monkeypatch):
    # The term is deleted while a correction for it is being decided: the delete (term and the
    # corrections that taught it) must win whole, before or after, never half.
    from sqlmodel import select

    from transcriptd.db import Correction

    st = client.app.state
    tid = client.post("/v1/vocab", json={"term": "Kubernetes"}).json()["id"]
    started = _delete_vocab_after_history_read(client, monkeypatch, tid)
    r = client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes"}).json()
    started[0].join()
    assert r["reason"] in ("learned heard_as", "term deleted")
    assert client.get("/v1/vocab").json() == []
    with st.db.session() as s:
        assert s.exec(select(Correction)).all() == []


def test_a_vocab_delete_after_the_history_read_is_not_undone_by_auto_learn(client, monkeypatch):
    # Two takes taught "Kubernetes"; a third correction reads that history, then the term (and
    # the history) is deleted. The stale history must not auto-learn the term back.
    from sqlmodel import select

    from transcriptd.db import Correction

    st = client.app.state
    client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes"})
    r = client.post("/v1/corrections", json={"heard": "cuba netties", "meant": "Kubernetes"}).json()
    assert r["reason"] == "auto-learned"
    [term] = client.get("/v1/vocab").json()
    started = _delete_vocab_after_history_read(client, monkeypatch, term["id"])
    client.post("/v1/corrections", json={"heard": "cooper nettys", "meant": "Kubernetes"})
    started[0].join()
    assert client.get("/v1/vocab").json() == []
    with st.db.session() as s:
        assert s.exec(select(Correction)).all() == []


def test_a_deleted_vocab_terms_id_is_never_given_to_the_next_term(client):
    # Ids are matched after the request that read them: a deleted newest term's id coming back
    # would hand its pending heard_as to an unrelated term.
    old = client.post("/v1/vocab", json={"term": "Kubernetes"}).json()["id"]
    assert client.delete(f"/v1/vocab/{old}").status_code == 204
    new = client.post("/v1/vocab", json={"term": "Postgres"}).json()["id"]
    assert new > old
    assert not client.app.state.db.add_heard_as(old, "kubernetes", "cooper netties")
    [t] = client.get("/v1/vocab").json()
    assert t["term"] == "Postgres" and t["heard_as"] == []


def test_a_vocab_reload_read_before_a_delete_is_not_installed_after_it(client, monkeypatch):
    # A correction's matcher reload reads the vocabulary, then a delete commits and reloads:
    # the correction's older read must not be installed over the delete's.
    import threading
    from types import SimpleNamespace

    from transcriptd.api import routes

    st = client.app.state
    tid = client.post("/v1/vocab", json={"term": "Kubernetes"}).json()["id"]
    real, started = st.db.vocab_terms, []

    def read_then_delete():
        rows = real()
        if not started:
            t = threading.Thread(target=routes.delete_vocab, args=(SimpleNamespace(app=client.app), tid))
            started.append(t)
            t.start()
            t.join(0.5)  # done by now unless this reload holds the matcher lock
        return rows

    monkeypatch.setattr(st.db, "vocab_terms", read_then_delete)
    r = client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes"}).json()
    started[0].join()
    assert r.get("learned")
    assert client.get("/v1/vocab").json() == []
    assert st.pipeline.matcher().entries == []
    r = client.post("/v1/transcribe", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.json()["text"] == "Never merge cooper netties"


def test_a_vocab_update_racing_its_delete_is_never_a_500(client, monkeypatch):
    # The term is deleted after the update read it: the update lands wholly before the delete
    # (or finds the term gone, 404), never a StaleDataError.
    import threading
    from types import SimpleNamespace

    from transcriptd.api import routes

    tid = client.post("/v1/vocab", json={"term": "Kubernetes"}).json()["id"]
    real, started = routes.normalize_key, []

    def key_then_delete(x):  # update_vocab normalizes heard_as after it read the term
        if not started:
            t = threading.Thread(target=routes.delete_vocab, args=(SimpleNamespace(app=client.app), tid))
            started.append(t)
            t.start()
            t.join(0.5)  # done by now unless the update holds the write lock
        return real(x)

    monkeypatch.setattr(routes, "normalize_key", key_then_delete)
    r = client.put(f"/v1/vocab/{tid}", json={"term": "Kubernetes", "heard_as": ["cooper netties"]})
    started[0].join()
    assert r.status_code in (200, 404), r.text
    assert client.get("/v1/vocab").json() == []
    assert client.put(f"/v1/vocab/{tid}", json={"term": "Kubernetes"}).status_code == 404


def test_a_vocab_update_deleted_right_after_its_commit_is_not_a_500(client, monkeypatch):
    # A delete landing between the update's commit and anything after it: the PUT still
    # answers with what it wrote, it never re-reads the row.
    from sqlmodel import Session

    from transcriptd.api import routes

    tid = client.post("/v1/vocab", json={"term": "Kubernetes"}).json()["id"]
    real_commit, done = Session.commit, []

    def commit_then_delete(self):
        real_commit(self)
        if not done:
            done.append(1)
            routes.delete_vocab(type("R", (), {"app": client.app})(), tid)

    monkeypatch.setattr(Session, "commit", commit_then_delete)
    r = client.put(f"/v1/vocab/{tid}", json={"term": "Kubernetes", "heard_as": ["cooper netties"]})
    monkeypatch.setattr(Session, "commit", real_commit)
    assert r.status_code == 200, r.text
    assert r.json()["heard_as"] == ["cooper netties"]
    assert client.get("/v1/vocab").json() == []


def test_a_correction_to_a_known_term_adds_its_heard_as(client):
    client.post("/v1/vocab", json={"term": "Kubernetes", "heard_as": ["cooper netties"]})
    r = client.post("/v1/corrections", json={"heard": "Cuba Netties", "meant": "kubernetes"}).json()
    assert r["reason"] == "learned heard_as" and r["learned"]
    [t] = client.get("/v1/vocab").json()
    assert t["term"] == "Kubernetes" and t["heard_as"] == sorted(["cooper netties", "cuba netties"])


def test_deleting_a_vocab_term_deletes_the_corrections_that_taught_it(client):
    from sqlmodel import select

    from transcriptd.db import Correction

    client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes"})
    client.post("/v1/corrections", json={"heard": "post gress", "meant": "Postgres"})
    tid = client.post("/v1/vocab", json={"term": "kubernetes"}).json()["id"]
    assert len(client.get("/v1/vocab").json()) == 1
    assert client.delete(f"/v1/vocab/{tid}").status_code == 204
    with client.app.state.db.session() as s:
        assert [c.meant for c in s.exec(select(Correction)).all()] == ["Postgres"]
    # One more correction is a first sighting again, not the second that auto-learns the term back.
    r = client.post("/v1/corrections", json={"heard": "cooper netties", "meant": "Kubernetes"}).json()
    assert r["suggest"] is True and r["times_seen"] == 1
    assert client.get("/v1/vocab").json() == []
