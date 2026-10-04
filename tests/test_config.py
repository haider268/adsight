from pathlib import Path

import pytest

from adsight.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("ADSIGHT_DATA_DIR", "ADSIGHT_SEED", "ADSIGHT_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_without_env_file(tmp_path: Path) -> None:
    assert not (tmp_path / ".env").exists()
    settings = get_settings()
    assert settings.data_dir == Path("data")
    assert settings.seed == 7
    assert settings.log_level == "INFO"
    assert settings.db_path == Path("data") / "adsight.duckdb"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADSIGHT_SEED", "11")
    monkeypatch.setenv("ADSIGHT_DATA_DIR", str(tmp_path / "elsewhere"))
    settings = Settings()
    assert settings.seed == 11
    assert settings.data_dir == tmp_path / "elsewhere"
    assert settings.db_path == tmp_path / "elsewhere" / "adsight.duckdb"


def test_get_settings_returns_fresh_instance() -> None:
    assert get_settings() is not get_settings()
