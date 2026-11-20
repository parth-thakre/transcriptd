"""The Silero VAD download (vad.ensure_model): pinned release, checked status and hash."""

import hashlib

import httpx
import pytest

from transcriptd import vad


def _fake_get(status: int, body: bytes):
    def get(url, **kw):
        assert url == vad.SILERO_URL and "/master/" not in url
        return httpx.Response(status, content=body, request=httpx.Request("GET", url))
    return get


def test_an_error_page_is_never_cached_as_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get(429, b"<html>rate limited</html>"))
    with pytest.raises(httpx.HTTPStatusError):
        vad.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_download_with_the_wrong_hash_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get(200, b"not the model"))
    with pytest.raises(RuntimeError, match="sha256"):
        vad.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_cached_file_with_the_wrong_hash_is_downloaded_again(tmp_path, monkeypatch):
    good = b"the pinned model"
    monkeypatch.setattr(vad, "SILERO_SHA256", hashlib.sha256(good).hexdigest())
    (tmp_path / "silero_vad.onnx").write_bytes(b"<html>cached error page</html>")
    monkeypatch.setattr(httpx, "get", _fake_get(200, good))
    p = vad.ensure_model(tmp_path)
    assert p.read_bytes() == good and sorted(f.name for f in tmp_path.iterdir()) == ["silero_vad.onnx"]
    # Once it matches, no download at all.
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("downloaded a good cached model again"))
    assert vad.ensure_model(tmp_path) == p


def test_two_first_downloads_at_once_both_succeed(tmp_path, monkeypatch):
    # A second ensure_model runs between the first one's temp write and its rename (two meetings
    # starting on a fresh install): neither may lose its temp file to the other.
    import os

    good = b"the pinned model"
    monkeypatch.setattr(vad, "SILERO_SHA256", hashlib.sha256(good).hexdigest())
    monkeypatch.setattr(httpx, "get", _fake_get(200, good))
    real_replace, raced = os.replace, []

    def replace(src, dst):
        if not raced:
            raced.append(True)
            assert vad.ensure_model(tmp_path).read_bytes() == good
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    p = vad.ensure_model(tmp_path)
    assert raced and p.read_bytes() == good
    assert [f.name for f in tmp_path.iterdir()] == ["silero_vad.onnx"]
