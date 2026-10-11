"""Prompts are byte-identical across runs, so recorded cassettes always replay."""

import asyncio
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from adsight.agent.context import model_tables, schema_card
from adsight.architectures import run_question
from adsight.architectures.common import format_result
from adsight.generate.context import build_context
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest, request_key
from adsight.semantic.model import SemanticModel, load_model
from adsight.sql_safety import open_agent_connection, run_sql

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
SPEC = "fixture:scripted"
GROUPED = "SELECT ad_id, SUM(spend) AS spend, SUM(leads) AS leads FROM daily_ad_metrics GROUP BY 1"
ORDERED = "SELECT ad_id, name FROM ads ORDER BY ad_id DESC"
S13_SQL = (
    "SELECT SUM(f.spend) AS spend, SUM(f.leads) AS leads FROM daily_ad_metrics f "
    "LEFT JOIN ads ad ON ad.ad_id = f.ad_id "
    "LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id "
    "WHERE ad_set.name = 'Homeowners 35-65' "
    "AND f.date BETWEEN DATE '2026-04-13' AND DATE '2026-04-26'"
)
WHY = 'Why did the cost per lead of the ad set "Homeowners 35-65" rise after 20 April 2026?'
TABLES = {"daily_ad_metrics", "ads", "ad_sets", "campaigns", "accounts", "change_log"}


@pytest.fixture(scope="module")
def con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = open_agent_connection(bench_data_dir / "adsight.duckdb")
    yield c
    c.close()


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return load_model(DEMO_YAML)


@pytest.fixture(scope="module")
def card(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> str:
    return schema_card(model, build_context(model, con), con)


def test_agent_connection_is_single_threaded(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute("SELECT current_setting('threads')").fetchone()
    assert row is not None and int(row[0]) == 1


def test_unordered_group_by_renders_identically(
    bench_data_dir: Path, con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # DuckDB refuses a second configuration on an open file: use a copy.
    db = tmp_path / "copy.duckdb"
    shutil.copyfile(bench_data_dir / "adsight.duckdb", db)
    parallel = duckdb.connect(str(db), read_only=True, config={"threads": 4})
    try:
        on_parallel = [run_sql(parallel, GROUPED, TABLES) for _ in range(20)]
    finally:
        parallel.close()
    on_agent = [format_result(run_sql(con, GROUPED, TABLES), 25) for _ in range(20)]
    assert len(set(on_agent)) == 1
    assert all(r.sorted_by_adsight and r.error is None for r in on_parallel)
    # Sorted on any connection: the keys come out in the same order everywhere.
    first = run_sql(con, GROUPED, TABLES)
    assert [r.rows[0][0] for r in on_parallel] == [first.rows[0][0]] * 20
    assert [row[0] for row in first.rows] == sorted(row[0] for row in first.rows)


def test_order_by_keeps_database_order(con: duckdb.DuckDBPyConnection) -> None:
    result = run_sql(con, ORDERED, TABLES)
    assert result.error is None and not result.sorted_by_adsight
    keys = [row[0] for row in result.rows]
    assert keys == sorted(keys, reverse=True)


def test_only_the_outermost_order_by_counts(con: duckdb.DuckDBPyConnection) -> None:
    inner = "WITH x AS (SELECT ad_id FROM ads ORDER BY ad_id DESC LIMIT 5) SELECT ad_id FROM x"
    result = run_sql(con, inner, TABLES)
    assert result.sorted_by_adsight
    assert [row[0] for row in result.rows] == sorted(row[0] for row in result.rows)
    outer = "WITH x AS (SELECT ad_id FROM ads) SELECT ad_id FROM x ORDER BY ad_id DESC"
    assert not run_sql(con, outer, TABLES).sorted_by_adsight


def test_nulls_and_mixed_types_sort_without_error(con: duckdb.DuckDBPyConnection) -> None:
    sql = "SELECT * FROM (VALUES (NULL), (2), (1)) t(v)"
    result = run_sql(con, sql, TABLES)
    assert result.rows == [(1,), (2,), (None,)]


def empty_answer() -> dict[str, Any]:
    return {
        "kind": "answer",
        "summary": "From the results.",
        "values": [],
        "entities": [],
        "direction": "none",
        "assumptions": [],
    }


def script_for(arch: str) -> Any:
    plan = {"kind": "query", "reason": "", "steps": [{"goal": "totals", "sql": GROUPED}]}

    def script(req: LLMRequest) -> dict[str, Any]:
        name = req.schema_name
        if name == "B0Plan":
            return {"kind": "query", "sql": GROUPED, "reason": ""}
        if name == "ReActStep":
            final = "Step 2:" in req.messages[-1].text
            return {
                "thought": "Totals by ad.",
                "action": "final" if final else "run_sql",
                "sql": "" if final else GROUPED,
                "final": empty_answer(),
            }
        if name == "ClassifyJSON":
            return {"kind": "diagnostic", "reason": "why question"}
        if name == "PlanJSON":
            if arch == "b4":
                return {"kind": "query", "reason": "", "steps": [{"goal": "s13", "sql": S13_SQL}]}
            return plan
        return empty_answer()

    return script


@pytest.mark.parametrize("arch", ["b0", "b1", "b2", "b3", "b4"])
def test_two_runs_send_identical_requests(
    arch: str, card: str, con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> None:
    keys = []
    for _ in range(2):
        provider = ScriptedProvider(script_for(arch))
        asyncio.run(
            run_question(
                arch, WHY, provider, SPEC, card, con, set(model_tables(model)), semantic=model
            )
        )
        assert provider.calls >= 2
        keys.append([request_key(r) for r in provider.requests])
    assert keys[0] == keys[1]
