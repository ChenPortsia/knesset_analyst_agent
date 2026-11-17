import pytest

from knesset.config import settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point logs at a temp dir and the db/Phoenix at closed ports, so tests touch nothing real."""
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DB_PORT", "1")
    monkeypatch.setenv("PHOENIX_URL", "http://127.0.0.1:1")
    settings.cache_clear()
    yield
    settings.cache_clear()
