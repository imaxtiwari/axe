"""Synthetic tests for file-backed settings opt-out, without application imports."""

from pathlib import Path

import pytest
from pydantic_settings.sources import DotEnvSettingsSource, SecretsSettingsSource

from axe.config import Settings, get_settings


def test_validation_never_reads_file_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AXE_VALIDATION", "1")
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    dotenv = tmp_path / ".env"
    dotenv.write_text("POLYGON_API_KEY=synthetic-dotenv-canary\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "polygon_api_key").write_text("synthetic-file-secret")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("File-backed settings source was read")

    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", forbidden)
    monkeypatch.setattr(SecretsSettingsSource, "find_case_path", forbidden)
    monkeypatch.chdir(tmp_path)
    assert Settings().polygon_api_key is None
    assert Settings(_env_file=dotenv, _secrets_dir=secrets).polygon_api_key is None
    get_settings.cache_clear()
    try:
        assert get_settings().polygon_api_key is None
    finally:
        get_settings.cache_clear()


def test_normal_dotenv_behavior_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AXE_VALIDATION", raising=False)
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    dotenv = tmp_path / ".env"
    dotenv.write_text("POLYGON_API_KEY=synthetic-dotenv-canary\n")
    assert Settings(_env_file=dotenv).polygon_api_key == "synthetic-dotenv-canary"


def test_preimport_storage_and_readiness(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from axe.db.session import async_engine
    from axe.main import create_app

    assert async_engine.url.database == "/scratch/data/axe.db"
    ready = tmp_path / "ready"
    app = create_app(Settings(readiness_dir=ready))
    assert not ready.exists()
    with TestClient(app) as client:
        assert ready.is_dir()
        assert client.get("/ready").status_code == 200
        assert (ready / ".ready").read_text() == "ok"
    assert not Path("/app/data").exists()
