from pathlib import Path

import pytest

# Settings.load creates data_dir/models_dir and writes data_dir/token relative to the working
# directory before a fixture gets to point them at tmp. Pin both first, so a test run from any
# directory never writes outside tmp (models go to the gitignored server/models cache, which
# also keeps Silero VAD from being downloaded once per test).
MODELS = Path(__file__).resolve().parents[1] / "models"


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPTD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRANSCRIPTD_MODELS_DIR", str(MODELS))
