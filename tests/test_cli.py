from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adsight.cli import app
from adsight.log import reset_logging

runner = CliRunner()


@pytest.fixture
def _drop_log_handlers() -> Iterator[None]:
    yield
    reset_logging()


def test_version_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "adsight 0.1.0" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
    assert "version" in result.output


@pytest.mark.usefixtures("_drop_log_handlers")
def test_data_builds_database(tmp_path: Path) -> None:
    result = runner.invoke(app, ["data", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "adsight.duckdb").is_file()
    assert (tmp_path / "manifest.json").is_file()
    assert "8534" in result.output
    for incident_id in ("I1", "I2", "I3", "I4", "I5"):
        assert incident_id in result.output
