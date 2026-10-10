"""Concurrent questions share one DuckDB connection; no query may see another's result."""

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from adsight.agent.context import model_tables, schema_card
from adsight.agent.diagnose import diagnose
from adsight.bench_run import RunConfig, RunStore, run_benchmark
from adsight.benchmark.questions import load_questions, save_questions
from adsight.generate.context import build_context
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest
from adsight.semantic.model import SemanticModel, load_model
from adsight.sql_safety import SqlResult, open_agent_connection, run_sql

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
QUERIES = {
    "SELECT SUM(f.leads) AS total_leads FROM daily_ad_metrics f": (["total_leads"], 1),
    "SELECT COUNT(*) AS n FROM daily_ad_metrics": (["n"], 1),
    "SELECT name FROM campaigns ORDER BY 1": (["name"], 9),
}
TABLES = {"daily_ad_metrics", "campaigns", "ads", "ad_sets", "accounts", "events_log"}


@pytest.fixture(scope="module")
def con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = open_agent_connection(bench_data_dir / "adsight.duckdb")
    yield c
    c.close()


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return load_model(DEMO_YAML)


def test_concurrent_run_sql_keeps_results_apart(con: duckdb.DuckDBPyConnection) -> None:
    sqls = list(QUERIES) * 10
    expected = {sql: run_sql(con, sql, TABLES).rows for sql in QUERIES}

    async def burst() -> list[SqlResult]:
        return await asyncio.gather(*(asyncio.to_thread(run_sql, con, s, TABLES) for s in sqls))

    for _ in range(5):
        results = asyncio.run(burst())
        for sql, result in zip(sqls, results, strict=True):
            columns, rows = QUERIES[sql]
            assert result.error is None, (sql, result.error)
            assert result.columns == columns, (sql, result.columns)
            assert result.row_count == rows, (sql, result.row_count)
            assert result.rows == expected[sql], sql


def test_concurrent_diagnose_stays_query_specific(
    con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    def case(name: str, start: str, end: str) -> tuple[str, list[dict[str, Any]]]:
        sql = (
            "SELECT SUM(f.spend) AS spend FROM daily_ad_metrics f "
            "LEFT JOIN ads ad ON ad.ad_id = f.ad_id "
            "LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id "
            f"WHERE ad_set.name = '{name}' AND f.date BETWEEN DATE '{start}' AND DATE '{end}'"
        )
        return f'Why did "{name}" change?', [{"sql": sql, "rows": []}]

    cases = [
        case("Homeowners 35-65", "2026-04-13", "2026-04-26"),
        case("Knee 45-65 Broad", "2026-05-11", "2026-05-31"),
        case("Retargeting Quotes", "2026-06-01", "2026-06-14"),
    ]
    expected = [diagnose(model, con, q, steps) for q, steps in cases]
    assert "55.00 -> 110.00" in expected[0] and "55.00 -> 110.00" not in expected[1]

    async def burst() -> list[str]:
        calls = [asyncio.to_thread(diagnose, model, con, q, s) for q, s in cases * 10]
        return await asyncio.gather(*calls)

    for _ in range(3):
        assert asyncio.run(burst()) == expected * 10


def test_bench_run_answers_see_only_their_own_results(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, tmp_path: Path
) -> None:
    questions = load_questions(DEMO_SET / "questions.jsonl")[:12]
    qdir = tmp_path / "twelve"
    save_questions(questions, qdir / "questions.jsonl")
    alias_of = {q.question: f"q_{q.id}" for q in questions}
    seen: dict[str, str] = {}

    def script(req: LLMRequest) -> dict[str, Any]:
        text = req.messages[-1].text
        question = next(q for q in alias_of if f"Question: {q}\n" in text)
        alias = alias_of[question]
        if req.schema_name == "B0Plan":
            offset = int(alias[-2:])
            sql = (
                f"SELECT COUNT(*) + {offset} AS {alias} FROM daily_ad_metrics f "
                f"WHERE f.leads >= {offset % 3}"
            )
            return {"kind": "query", "sql": sql, "reason": ""}
        seen[alias] = text
        return {
            "kind": "answer",
            "summary": "ok",
            "values": [],
            "entities": [],
            "direction": "none",
            "assumptions": [],
        }

    card = schema_card(model, build_context(model, con), con)
    store = RunStore(tmp_path / "runs.sqlite")
    cfg = RunConfig(qdir, "b0", "fixture:scripted", workers=12)
    outcome = asyncio.run(
        run_benchmark(
            cfg, ScriptedProvider(script), card, con, set(model_tables(model)), store, tmp_path
        )
    )
    store.close()
    assert len(outcome.ran) == 12 and len(seen) == 12
    for alias, text in seen.items():
        assert f"Columns: {alias}\n" in text, alias
        others = set(re.findall(r"q_[A-Z]{2}\d{2}", text)) - {alias}
        assert not others, (alias, others)
