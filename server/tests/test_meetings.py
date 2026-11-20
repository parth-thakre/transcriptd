import io
import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from transcriptd.app import create_app
from transcriptd.config import Settings
from transcriptd.segmenter import StreamSegmenter
from transcriptd.vad import SileroVAD, ensure_model
from tests.test_pipeline import FakeEngine, _w

MODELS = __import__("pathlib").Path(__file__).resolve().parents[1] / "models"


FIXTURES = __import__("pathlib").Path(__file__).resolve().parent / "fixtures"


def _speech_with_gaps():
    """Real speech (JFK clip) with 1 s of silence spliced in the middle so the VAD has
    a clear boundary to split on."""
    pcm, rate = sf.read(FIXTURES / "jfk.wav", dtype="float32")
    assert rate == 16000
    gap = np.zeros(16000, dtype=np.float32)
    half = len(pcm) // 2
    return np.concatenate([pcm[:half], gap, pcm[half:]]).astype(np.float32)


@pytest.fixture(scope="module")
def vad():
    return SileroVAD(ensure_model(MODELS))


def test_segmenter_splits_on_silence(vad):
    seg = StreamSegmenter(vad, min_silence_ms=400)
    pcm = _speech_with_gaps()
    out = []
    for i in range(0, len(pcm), 320):
        out.extend(seg.feed(pcm[i : i + 320]))
    out.extend(seg.flush())
    spans = [(s.start_ms, s.end_ms) for s in out]
    assert 2 <= len(out) <= 6, spans
    # Something ends before the spliced gap and something starts after it.
    gap_start = len(pcm) // 2 * 1000 // 16000 - 500
    assert any(e <= gap_start + 700 for _, e in spans) and any(s >= gap_start + 300 for s, _ in spans), spans
    total = sum(e - s for s, e in spans)
    assert total >= 8000, spans  # most of the 11 s of speech is covered
    for s in out:
        assert len(s.pcm) == pytest.approx((s.end_ms - s.start_ms) * 16, abs=600)


@pytest.fixture
def client(tmp_path):
    s = Settings.load(tmp_path / "nope.toml")
    s.data_dir = tmp_path
    s.models_dir = MODELS
    app = create_app(s)
    with TestClient(app) as c:
        app.state.registry._engines["english"] = FakeEngine(_w("we will not ship before friday"))
        c.headers["Authorization"] = f"Bearer {s.token}"
        yield c


def test_meeting_lifecycle_stream_and_upload(client):
    m = client.post("/v1/meetings", json={"title": "Standup"}).json()
    assert m["status"] == "recording"
    mid = m["id"]

    pcm = _speech_with_gaps()[: 16000 * 5]
    stereo = np.zeros(len(pcm) * 2, dtype="<i2")
    stereo[0::2] = (pcm * 32767).astype("<i2")  # me talks, them silent
    with client.websocket_connect(f"/v1/meetings/{mid}/stream?token={client.app.state.settings.token}") as ws:
        for i in range(0, len(stereo), 640):
            ws.send_bytes(stereo[i : i + 640].tobytes())
        ws.send_text(json.dumps({"type": "end"}))
        got = []
        while True:
            j = ws.receive_json()
            got.append(j)
            if j["type"] == "ended":
                break
    segs = [g for g in got if g["type"] == "segment"]
    assert segs and all(s["channel"] == "me" for s in segs)
    assert segs[0]["text"] == "We will not ship before friday"

    detail = client.get(f"/v1/meetings/{mid}").json()
    assert detail["status"] == "done" and len(detail["segments"]) == len(segs)
    assert all(not s["final"] for s in detail["segments"])

    # Upload the "backup" WAV: final pass replaces live segments.
    buf = io.BytesIO()
    sf.write(buf, np.stack([pcm, np.zeros_like(pcm)], axis=1), 16000, format="WAV", subtype="PCM_16")
    r = client.post(f"/v1/meetings/{mid}/audio", files={"file": ("m.wav", buf.getvalue(), "audio/wav")})
    assert r.status_code == 200
    detail = client.get(f"/v1/meetings/{mid}").json()
    assert detail["status"] == "done"
    assert detail["segments"] and all(s["final"] for s in detail["segments"])

    txt = client.get(f"/v1/meetings/{mid}/transcript").text
    assert "me: We will not ship before friday" in txt
    assert client.get("/v1/meetings", params={"q": "friday"}).json()[0]["id"] == mid

    r = client.post(f"/v1/meetings/{mid}/summarize")
    assert r.status_code == 400  # no LLM configured

    client.patch(f"/v1/meetings/{mid}", json={"notes": "hello"})
    assert client.get(f"/v1/meetings/{mid}").json()["notes"] == "hello"
    assert client.delete(f"/v1/meetings/{mid}").status_code == 204


def _stereo_wav(seconds=1.0):
    buf = io.BytesIO()
    sf.write(buf, np.zeros((int(16000 * seconds), 2), dtype=np.float32), 16000, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_meeting_uploads_and_the_processing_queue_are_bounded(client):
    st = client.app.state
    mid = client.post("/v1/meetings", json={}).json()["id"]
    wav = st.settings.data_dir / "meetings" / f"{mid}.wav"

    st.settings.max_meeting_upload_mb = 32 / 1024  # 32 KB; 1 s of stereo is ~64 KB
    r = client.post(f"/v1/meetings/{mid}/audio", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
    assert r.status_code == 413 and not wav.exists()
    assert len(st.meeting_jobs) == 0  # the refused upload gave its queue slot back
    st.settings.max_meeting_upload_mb = 500

    st.settings.max_queued_meetings = 1
    st.meeting_jobs.claim(9999, 1)  # another meeting is being processed
    r = client.post(f"/v1/meetings/{mid}/audio", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
    assert r.status_code == 429 and r.headers["retry-after"] == "60" and not wav.exists()
    r = client.post(f"/v1/meetings/{mid}/audio?process=false", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
    assert r.status_code == 200 and wav.exists()  # storing without processing takes no slot
    assert client.post(f"/v1/meetings/{mid}/process").status_code == 429
    st.meeting_jobs.release(9999)

    st.meeting_jobs.claim(mid, 8)  # this meeting's last upload is still processing
    assert client.post(f"/v1/meetings/{mid}/process").status_code == 409
    st.meeting_jobs.release(mid)
    r = client.post(f"/v1/meetings/{mid}/audio", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
    assert r.status_code == 200 and r.json()["bytes"] == len(_stereo_wav())
    assert len(st.meeting_jobs) == 0  # released when the pass finished
    assert client.get(f"/v1/meetings/{mid}").json()["status"] == "done"


def test_stop_worker_always_awaits_the_live_worker():
    import asyncio

    from transcriptd.api.meetings import _stop_worker

    async def run():
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        seen = []

        async def worker():
            while (item := await q.get()) is not None:
                seen.append(item)

        w = asyncio.create_task(worker())
        await q.put("a")
        await _stop_worker(w, q, timeout=1)
        assert w.done() and seen == ["a"]

        stuck = asyncio.create_task(asyncio.sleep(10))
        await _stop_worker(stuck, asyncio.Queue(maxsize=1), timeout=0.05)
        assert stuck.cancelled()

    asyncio.run(run())


def _segments(st, mid=None):
    from sqlmodel import select

    from transcriptd.db import MeetingSegment

    with st.db.session() as s:
        q = select(MeetingSegment)
        return s.exec(q if mid is None else q.where(MeetingSegment.meeting_id == mid)).all()


def test_a_meeting_deleted_during_its_upload_keeps_no_audio_and_frees_its_slot(client, monkeypatch):
    from transcriptd.api import meetings as api

    st = client.app.state
    mid = wav = None
    real = api._save_upload

    def save_then_delete(*a, **kw):
        n = real(*a, **kw)
        assert wav.exists()
        assert client.delete(f"/v1/meetings/{mid}").status_code == 204
        return n

    monkeypatch.setattr(api, "_save_upload", save_then_delete)
    for process in ("true", "false"):
        mid = client.post("/v1/meetings", json={}).json()["id"]
        wav = st.settings.data_dir / "meetings" / f"{mid}.wav"
        r = client.post(f"/v1/meetings/{mid}/audio?process={process}", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
        assert r.status_code == 404 and not wav.exists()
        assert len(st.meeting_jobs) == 0


def test_a_meeting_deleted_while_processed_gets_no_segments(client, monkeypatch):
    from transcriptd import meetings
    from transcriptd.db import Meeting

    st = client.app.state
    mid = client.post("/v1/meetings", json={}).json()["id"]
    pcm = _speech_with_gaps()[: 16000 * 5]
    buf = io.BytesIO()
    sf.write(buf, np.stack([pcm, np.zeros_like(pcm)], axis=1), 16000, format="WAV", subtype="PCM_16")
    assert client.post(f"/v1/meetings/{mid}/audio?process=false", files={"file": ("m.wav", buf.getvalue(), "audio/wav")}).status_code == 200
    real = meetings.transcribe_channel

    def transcribe_then_delete(*a, **kw):
        out = real(*a, **kw)
        if a[3] == "them":
            assert client.delete(f"/v1/meetings/{mid}").status_code == 204
        return out

    monkeypatch.setattr(meetings, "transcribe_channel", transcribe_then_delete)
    meetings.process_meeting(st.pipeline, st.db, st.settings, mid)  # no error: it just stores nothing
    assert _segments(st, mid) == []
    with st.db.session() as s:
        assert s.get(Meeting, mid) is None
    assert client.post(f"/v1/meetings/{mid}/summarize").status_code == 404
    assert client.post("/v1/meetings/424242/summarize").status_code == 404


def test_live_segments_of_a_deleted_meeting_are_not_stored(client):
    mid = client.post("/v1/meetings", json={}).json()["id"]
    pcm = _speech_with_gaps()[: 16000 * 5]
    stereo = np.zeros(len(pcm) * 2, dtype="<i2")
    stereo[0::2] = (pcm * 32767).astype("<i2")
    with client.websocket_connect(f"/v1/meetings/{mid}/stream") as ws:
        assert client.delete(f"/v1/meetings/{mid}").status_code == 204
        for i in range(0, len(stereo), 640):
            ws.send_bytes(stereo[i : i + 640].tobytes())
        ws.send_text(json.dumps({"type": "end"}))
        got = [ws.receive_json()]
        while got[-1]["type"] != "ended":
            got.append(ws.receive_json())
    assert [g["type"] for g in got] == ["ended"]
    assert _segments(client.app.state) == []


def test_an_upload_attaching_while_its_meeting_is_deleted_leaves_no_audio(client):
    """The delete reads the meeting's audio_path in the statement that deletes it: audio
    attached just before the delete starts is still removed with it."""
    from sqlalchemy import event

    from transcriptd.api import meetings as api

    st = client.app.state
    mid = client.post("/v1/meetings", json={}).json()["id"]
    folder = st.settings.data_dir / "meetings"
    folder.mkdir(exist_ok=True)
    wav = folder / f"{mid}.wav"
    attached = []

    def attach_first(conn, cursor, statement, params, context, executemany):
        if not attached and statement.lstrip().upper().startswith("DELETE"):
            wav.write_bytes(_stereo_wav())  # an upload saved its file and attaches it now,
            attached.append(api._attach_audio(st.db, mid, wav, False))  # before any of the delete's writes

    event.listen(st.db.engine, "before_cursor_execute", attach_first)
    try:
        assert client.delete(f"/v1/meetings/{mid}").status_code == 204
    finally:
        event.remove(st.db.engine, "before_cursor_execute", attach_first)
    assert attached == [True] and not wav.exists()


def test_deleting_a_meeting_removes_its_upload_files_even_when_unattached(client):
    st = client.app.state
    mid = client.post("/v1/meetings", json={}).json()["id"]
    folder = st.settings.data_dir / "meetings"
    folder.mkdir(exist_ok=True)
    wav, part = folder / f"{mid}.wav", folder / f".{mid}.wav.part"
    wav.write_bytes(b"saved, not attached yet")
    part.write_bytes(b"still uploading")
    assert client.delete(f"/v1/meetings/{mid}").status_code == 204
    assert not wav.exists() and not part.exists()
    assert client.delete(f"/v1/meetings/{mid}").status_code == 404


def test_a_failed_attach_gives_the_processing_slot_back(client, monkeypatch):
    from transcriptd.api import meetings as api

    st = client.app.state
    mid = client.post("/v1/meetings", json={}).json()["id"]
    real = api._attach_audio

    def broken(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(api, "_attach_audio", broken)
    with pytest.raises(RuntimeError):
        client.post(f"/v1/meetings/{mid}/audio", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
    assert len(st.meeting_jobs) == 0
    monkeypatch.setattr(api, "_attach_audio", real)
    r = client.post(f"/v1/meetings/{mid}/audio", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
    assert r.status_code == 200 and r.json()["processing"]  # not a 409 until restart
    assert len(st.meeting_jobs) == 0


def test_a_deleted_meetings_id_is_never_given_to_a_new_one(client):
    a = client.post("/v1/meetings", json={}).json()["id"]
    b = client.post("/v1/meetings", json={}).json()["id"]
    assert client.delete(f"/v1/meetings/{b}").status_code == 204
    c = client.post("/v1/meetings", json={}).json()["id"]
    assert c > b > a
    # The agent's late upload for the deleted meeting finds nothing, not the new meeting.
    r = client.post(f"/v1/meetings/{b}/audio?process=false", files={"file": ("m.wav", _stereo_wav(), "audio/wav")})
    assert r.status_code == 404
    assert client.get(f"/v1/meetings/{c}").json()["has_audio"] is False
