import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from adsight.log import (
    JsonFormatter,
    configure_logging,
    get_logger,
    get_run_id,
    set_run_id,
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    set_run_id(None)
    yield
    set_run_id(None)
    logger = logging.getLogger("adsight")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _record(msg: str, **extra: object) -> logging.LogRecord:
    logger = get_logger("test")
    return logger.makeRecord(logger.name, logging.INFO, __file__, 1, msg, None, None, extra=extra)


def _format(msg: str, **extra: object) -> dict[str, object]:
    line = JsonFormatter().format(_record(msg, **extra))
    assert "\n" not in line
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def test_one_json_line_with_required_keys() -> None:
    data = _format("hello")
    assert {"ts", "level", "logger", "msg", "run_id"} <= data.keys()
    assert data["level"] == "INFO"
    assert data["logger"] == "adsight.test"
    assert data["msg"] == "hello"
    ts = data["ts"]
    assert isinstance(ts, str)
    assert ts.endswith("Z")


def test_run_id_null_by_default_then_set() -> None:
    assert get_run_id() is None
    assert _format("a")["run_id"] is None
    set_run_id("run-123")
    assert get_run_id() == "run-123"
    assert _format("b")["run_id"] == "run-123"


def test_extra_fields_merged_at_top_level() -> None:
    data = _format("q", fields={"rows": 3, "arch": "baseline"})
    assert data["rows"] == 3
    assert data["arch"] == "baseline"
    assert "fields" not in data


def test_non_serialisable_value_is_stringified() -> None:
    data = _format("p", fields={"path": Path("data") / "x.duckdb"})
    assert data["path"] == str(Path("data") / "x.duckdb")


def test_configure_twice_gives_one_stderr_handler() -> None:
    configure_logging()
    configure_logging()
    handlers = logging.getLogger("adsight").handlers
    streams = [h for h in handlers if type(h) is logging.StreamHandler]
    assert len(streams) == 1
    assert len(handlers) == 1


def test_stderr_output_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    get_logger("cli").info("to stderr", extra={"fields": {"n": 1}})
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    data = json.loads(err[0])
    assert data["msg"] == "to stderr"
    assert data["n"] == 1


def test_file_output_appends_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "run.jsonl"
    configure_logging(path=path)
    get_logger("x").info("first")
    configure_logging(path=path)
    get_logger("x").warning("second", extra={"fields": {"k": "v"}})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["msg"] for line in lines] == ["first", "second"]
    assert json.loads(lines[1])["k"] == "v"
    assert json.loads(lines[1])["level"] == "WARNING"
