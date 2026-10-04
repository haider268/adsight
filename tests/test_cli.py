from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adsight.benchmark.questions import load_questions, save_questions
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


def test_bench_refs_ok(bench_data_dir: Path, questions_path: Path, sql_dir: Path) -> None:
    result = runner.invoke(app, _refs_args(questions_path, sql_dir, bench_data_dir))
    assert result.exit_code == 0, result.output
    assert "MISMATCH" not in result.output


def test_bench_refs_detects_tampering(
    bench_data_dir: Path, questions_path: Path, sql_dir: Path, tmp_path: Path
) -> None:
    questions = load_questions(questions_path)
    lk01 = questions[0]
    assert lk01.id == "LK01" and lk01.expected is not None
    tampered = lk01.model_copy(
        update={"expected": lk01.expected.model_copy(update={"values": {"total_spend": 1.0}})}
    )
    copy = tmp_path / "questions.jsonl"
    save_questions([tampered, *questions[1:]], copy)
    result = runner.invoke(app, _refs_args(copy, sql_dir, bench_data_dir))
    assert result.exit_code == 1
    assert "MISMATCH" in result.output


def test_bench_refs_missing_database(questions_path: Path, sql_dir: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, _refs_args(questions_path, sql_dir, tmp_path / "empty"))
    assert result.exit_code == 2


def _refs_args(questions: Path, sql_dir: Path, data_dir: Path) -> list[str]:
    return [
        "bench",
        "refs",
        "--questions",
        str(questions),
        "--sql-dir",
        str(sql_dir),
        "--data-dir",
        str(data_dir),
    ]


def test_bench_refs_write_fills_expected(
    bench_data_dir: Path, questions_path: Path, sql_dir: Path, tmp_path: Path
) -> None:
    bare = [q.model_copy(update={"expected": None}) for q in load_questions(questions_path)]
    copy = tmp_path / "questions.jsonl"
    save_questions(bare, copy)
    assert runner.invoke(app, _refs_args(copy, sql_dir, bench_data_dir)).exit_code == 1
    written = runner.invoke(app, [*_refs_args(copy, sql_dir, bench_data_dir), "--write"])
    assert written.exit_code == 0, written.output
    assert copy.read_text(encoding="utf-8") == questions_path.read_text(encoding="utf-8")
    assert runner.invoke(app, _refs_args(copy, sql_dir, bench_data_dir)).exit_code == 0
