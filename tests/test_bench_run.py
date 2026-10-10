import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from adsight.agent.context import model_tables, schema_card
from adsight.bench_run import EXIT_FAILED, EXIT_OK, EXIT_STOPPED, RunConfig, RunStore, run_benchmark
from adsight.benchmark.questions import load_questions, save_questions
from adsight.cli import app
from adsight.config import Settings
from adsight.generate.context import build_context
from adsight.llm.base import DailyLimitReached
from adsight.llm.factory import make_provider
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest
from adsight.semantic.model import load_model
from adsight.sql_safety import open_agent_connection

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
EFFECTIVE = ["LK01", "LK02", "AG01", "TR01", "AM01", "UN01"]
SPEC = "fixture:scripted"

runner = CliRunner()


@pytest.fixture(scope="module")
def con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = open_agent_connection(bench_data_dir / "adsight.duckdb")
    yield c
    c.close()


@pytest.fixture(scope="module")
def card(con: duckdb.DuckDBPyConnection) -> str:
    model = load_model(DEMO_YAML)
    return schema_card(model, build_context(model, con), con)


TABLES = set(model_tables(load_model(DEMO_YAML)))


@pytest.fixture
def qdir(tmp_path: Path) -> Path:
    """Six effective questions, plus one rejected and one pending that must never run."""
    by_id = {q.id: q for q in load_questions(DEMO_SET / "questions.jsonl")}
    assert by_id["RC17"].review == "rejected"
    pending = by_id["RC10"].model_copy(update={"review": "pending"})
    folder = tmp_path / "six"
    chosen = [*(by_id[i] for i in EFFECTIVE), by_id["RC17"], pending]
    save_questions(chosen, folder / "questions.jsonl")
    return folder


@pytest.fixture
def store(tmp_path: Path) -> Iterator[RunStore]:
    s = RunStore(tmp_path / "state" / "runs.sqlite")
    yield s
    s.close()


def texts(qdir: Path) -> dict[str, str]:
    return {q.id: q.question for q in load_questions(qdir / "questions.jsonl")}


class Killed(BaseException):
    """Stands for the process being killed (like Ctrl-C): not a per-question failure."""


def refuse_all(req: LLMRequest) -> dict[str, Any]:
    return {"kind": "cannot_answer", "sql": "", "reason": "scripted"}


def failing_on(question_text: str, error: BaseException) -> Any:
    def script(req: LLMRequest) -> dict[str, Any]:
        if question_text in req.messages[-1].text:
            raise error
        return refuse_all(req)

    return script


def go(cfg: RunConfig, provider: Any, card: str, con: Any, store: RunStore, tmp: Path) -> Any:
    return asyncio.run(run_benchmark(cfg, provider, card, con, TABLES, store, tmp / "reports"))


def statuses(store: RunStore, run_id: str) -> dict[str, str]:
    return {r["question_id"]: r["status"] for r in store.results(run_id)}


def test_run_completes_and_writes_a_report(
    qdir: Path, card: str, con: Any, store: RunStore, tmp_path: Path
) -> None:
    provider = ScriptedProvider(refuse_all)
    cfg = RunConfig(qdir, "b0", SPEC)
    outcome = go(cfg, provider, card, con, store, tmp_path)
    assert outcome.exit_code == EXIT_OK
    assert outcome.run_id == "b0-fixture-scripted-six"
    assert statuses(store, outcome.run_id) == dict.fromkeys(EFFECTIVE, "done")
    report = json.loads((tmp_path / "reports" / f"{outcome.run_id}.json").read_text())
    assert report["status"] == "complete" and report["questions"] == 6
    assert report["per_category"]["unanswerable"] == {"passed": 1, "total": 1}
    assert report["per_category"]["lookup"] == {"passed": 0, "total": 2}
    assert report["passed"] == 1 and report["accuracy"] == pytest.approx(1 / 6, abs=1e-4)
    assert report["mean_llm_calls"] == 1.0
    sent = " ".join(r.messages[-1].text for r in provider.requests)
    for qid in ("RC17", "RC10"):
        assert texts(qdir)[qid] not in sent
    again = go(cfg, ScriptedProvider(refuse_all), card, con, store, tmp_path)
    assert again.ran == [] and again.exit_code == EXIT_OK


def test_a_killed_run_resumes_where_it_stopped(
    qdir: Path, card: str, con: Any, store: RunStore, tmp_path: Path
) -> None:
    cfg = RunConfig(qdir, "b1", SPEC, workers=1)
    killer = ScriptedProvider(failing_on(texts(qdir)["TR01"], Killed("killed")))
    with pytest.raises(Killed):
        go(cfg, killer, card, con, store, tmp_path)
    run_id = cfg.resolved_run_id
    assert statuses(store, run_id) == dict.fromkeys(["LK01", "LK02", "AG01"], "done")

    def final(req: LLMRequest) -> dict[str, Any]:
        empty = {"kind": "cannot_answer", "summary": "scripted", "values": [], "entities": []}
        return {
            "thought": "",
            "action": "final",
            "sql": "",
            "final": {**empty, "direction": "none", "assumptions": []},
        }

    resumed = ScriptedProvider(final)
    outcome = go(cfg, resumed, card, con, store, tmp_path)
    assert outcome.ran == ["TR01", "AM01", "UN01"]
    assert resumed.calls == 3
    assert statuses(store, run_id) == dict.fromkeys(EFFECTIVE, "done")


def test_daily_limit_stops_cleanly_and_resumes(
    qdir: Path, card: str, con: Any, store: RunStore, tmp_path: Path
) -> None:
    cfg = RunConfig(qdir, "b0", SPEC, workers=1)
    limit = DailyLimitReached("groq", "openai/gpt-oss-120b", "try again in 7m12s")
    provider = ScriptedProvider(failing_on(texts(qdir)["AG01"], limit))
    outcome = go(cfg, provider, card, con, store, tmp_path)
    assert outcome.exit_code == EXIT_STOPPED
    assert outcome.stop_reason is not None and "try again in 7m12s" in outcome.stop_reason
    assert statuses(store, cfg.resolved_run_id) == {
        "LK01": "done",
        "LK02": "done",
        "AG01": "retry",
        "TR01": "retry",
        "AM01": "retry",
        "UN01": "retry",
    }
    assert outcome.report["status"] == "stopped"
    rerun = go(cfg, ScriptedProvider(refuse_all), card, con, store, tmp_path)
    assert rerun.exit_code == EXIT_OK
    assert rerun.ran == ["AG01", "TR01", "AM01", "UN01"]
    assert statuses(store, cfg.resolved_run_id) == dict.fromkeys(EFFECTIVE, "done")


def test_replay_with_empty_cache_fails_fast(
    qdir: Path, card: str, con: Any, store: RunStore, tmp_path: Path
) -> None:
    settings = Settings(_env_file=None, state_dir=tmp_path / "state")
    provider = make_provider(SPEC, settings, mode="replay", script=[])
    cfg = RunConfig(qdir, "b0", SPEC, workers=1)
    outcome = go(cfg, provider, card, con, store, tmp_path)
    provider.cache.close()
    assert outcome.exit_code == EXIT_FAILED
    rows = store.results(cfg.resolved_run_id)
    assert [(r["question_id"], r["status"]) for r in rows] == [("LK01", "error")]
    assert rows[0]["error"].startswith("cache_miss: ")


def test_limit_and_batch_workers(
    qdir: Path, card: str, con: Any, store: RunStore, tmp_path: Path
) -> None:
    limited = RunConfig(qdir, "b0", SPEC, limit=2)
    outcome = go(limited, ScriptedProvider(refuse_all), card, con, store, tmp_path)
    assert sorted(outcome.ran) == ["LK01", "LK02"]
    batch = RunConfig(qdir, "b0", SPEC, batch=True, run_id="batched")
    outcome = go(batch, ScriptedProvider(refuse_all), card, con, store, tmp_path)
    assert sorted(outcome.ran) == sorted(EFFECTIVE)


# --- CLI --------------------------------------------------------------------------------


def test_cli_run_replay_and_status(
    qdir: Path, bench_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    import adsight.cli

    monkeypatch.setattr(adsight.cli, "console", Console(width=200))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADSIGHT_STATE_DIR", str(tmp_path / "state"))
    args = [
        "bench",
        "run",
        "--questions-dir",
        str(qdir),
        "--arch",
        "b0",
        "--model",
        SPEC,
        "--replay",
        "--semantic",
        str(DEMO_YAML),
        "--db",
        str(bench_data_dir / "adsight.duckdb"),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == EXIT_FAILED, result.output
    assert "b0-fixture-scripted-six" in result.output
    assert "judge: off" in result.output
    assert (tmp_path / "state" / "reports" / "b0-fixture-scripted-six.json").is_file()
    status = runner.invoke(app, ["bench", "status"])
    assert status.exit_code == 0, status.output
    assert "b0-fixture-scripted-six" in status.output and "failed" in status.output
    one = runner.invoke(app, ["bench", "status", "--run-id", "nope"])
    assert one.exit_code == 0 and "b0-fixture-scripted-six" not in one.output
    bad = runner.invoke(app, [*args[:6], "b9", *args[7:]])
    assert bad.exit_code == 2


def test_one_failing_question_never_stops_the_run(
    qdir: Path, card: str, con: Any, store: RunStore, tmp_path: Path
) -> None:
    cfg = RunConfig(qdir, "b0", SPEC, workers=1)
    broken = ScriptedProvider(failing_on(texts(qdir)["AG01"], RuntimeError("bad\nsecond line")))
    outcome = go(cfg, broken, card, con, store, tmp_path)
    rows = {r["question_id"]: r for r in store.results(cfg.resolved_run_id)}
    assert rows["AG01"]["status"] == "error"
    assert rows["AG01"]["error"] == "RuntimeError: bad"
    assert {q: r["status"] for q, r in rows.items() if q != "AG01"} == dict.fromkeys(
        ["LK01", "LK02", "TR01", "AM01", "UN01"], "done"
    )
    assert outcome.exit_code == EXIT_OK and outcome.report["status"] == "incomplete"
