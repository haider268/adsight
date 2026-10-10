from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from adsight.benchmark.questions import load_questions
from adsight.cli import app
from adsight.data.build import full_weeks, open_readonly
from adsight.generate.generator import generate_benchmark
from adsight.semantic.inspect import draft_model
from adsight.semantic.model import SemanticModel, load_model

REPO_ROOT = Path(__file__).resolve().parents[1]
FLAT_YAML = REPO_ROOT / "semantic" / "flat-demo.yaml"
COMMITTED = REPO_ROOT / "benchmark" / "generated" / "adsight-flat-demo"

runner = CliRunner()


@pytest.fixture(scope="module")
def flat_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data_dir = tmp_path_factory.mktemp("flat-data")
    result = runner.invoke(
        app, ["data", "--shape", "flat-weekly", "--seed", "7", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    return data_dir / "flat" / "adsight_flat.duckdb"


@pytest.fixture(scope="module")
def flat_con(flat_db: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(flat_db)
    yield con
    con.close()


@pytest.fixture(scope="module")
def demo_con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(bench_data_dir / "adsight.duckdb")
    yield con
    con.close()


@pytest.fixture(scope="module")
def flat_generated(
    tmp_path_factory: pytest.TempPathFactory, flat_con: duckdb.DuckDBPyConnection
) -> Path:
    out = tmp_path_factory.mktemp("flat-gen")
    generate_benchmark(load_model(FLAT_YAML), flat_con, 7, out)
    return out


def _window() -> tuple[str, str]:
    weeks = full_weeks()
    return weeks[0].isoformat(), (weeks[-1] + timedelta(days=6)).isoformat()


def test_full_weeks_are_mondays_inside_the_window() -> None:
    weeks = full_weeks()
    assert all(w.weekday() == 0 for w in weeks)
    assert weeks[0].isoformat() == "2026-01-05"
    assert (weeks[-1] + timedelta(days=6)).isoformat() == "2026-06-28"


def test_one_table_with_google_style_columns(flat_con: duckdb.DuckDBPyConnection) -> None:
    tables = [r[0] for r in flat_con.execute("SHOW TABLES").fetchall()]
    assert tables == ["perf"]
    columns = [(r[0], r[1]) for r in flat_con.execute("DESCRIBE perf").fetchall()]
    assert columns == [
        ("week_start", "DATE"),
        ("campaign_id", "VARCHAR"),
        ("campaign_name", "VARCHAR"),
        ("ad_group", "VARCHAR"),
        ("cost", "DOUBLE"),
        ("impr", "BIGINT"),
        ("clicks", "BIGINT"),
        ("conversions", "BIGINT"),
    ]


def test_row_count_is_campaign_ad_set_weeks_present(
    flat_con: duckdb.DuckDBPyConnection, demo_con: duckdb.DuckDBPyConnection
) -> None:
    first, last = _window()
    expected = demo_con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT s.campaign_id, s.ad_set_id, "
        "date_trunc('week', m.date) FROM daily_ad_metrics m JOIN ads a USING (ad_id) "
        "JOIN ad_sets s USING (ad_set_id) WHERE m.date BETWEEN ? AND ?)",
        [first, last],
    ).fetchone()
    got = flat_con.execute("SELECT COUNT(*) FROM perf").fetchone()
    assert expected is not None and got is not None
    assert got[0] == expected[0]


def test_totals_match_the_daily_demo(
    flat_con: duckdb.DuckDBPyConnection, demo_con: duckdb.DuckDBPyConnection
) -> None:
    first, last = _window()
    daily = demo_con.execute(
        "SELECT SUM(spend), SUM(impressions), SUM(clicks), SUM(leads) FROM daily_ad_metrics "
        "WHERE date BETWEEN ? AND ?",
        [first, last],
    ).fetchone()
    flat = flat_con.execute(
        "SELECT SUM(cost), SUM(impr), SUM(clicks), SUM(conversions) FROM perf"
    ).fetchone()
    assert daily is not None and flat is not None
    assert round(flat[0], 2) == float(daily[0])
    assert tuple(flat[1:]) == tuple(daily[1:])


def test_inspect_matches_committed_flat_yaml(flat_con: duckdb.DuckDBPyConnection) -> None:
    drafted: SemanticModel = draft_model(flat_con, "adsight-flat-demo")[0]
    committed = load_model(FLAT_YAML)
    assert drafted == committed
    assert committed.grain == "week"
    assert committed.change_log is None and committed.attributes == []
    assert [lv.name for lv in committed.levels] == ["campaign", "ad_set"]


def test_flat_generation_skips_what_the_data_lacks(flat_generated: Path) -> None:
    questions = load_questions(flat_generated / "questions.jsonl")
    assert questions
    for q in questions:
        assert q.template not in ("L_DAILY", "A_WEEKDAY"), q.id
        assert q.params.get("metric") not in ("frequency", "link_clicks", "reach"), q.id
        assert "link click" not in q.question, q.id
    for sql in (flat_generated / "reference_sql").glob("*.sql"):
        text = sql.read_text(encoding="utf-8").lower()
        assert "link" not in text and "reach" not in text, sql.name


def test_committed_flat_benchmark_regenerates(flat_generated: Path) -> None:
    for name in ("questions.jsonl", "lint_report.json", "context.json"):
        assert (flat_generated / name).read_bytes() == (COMMITTED / name).read_bytes(), name


def test_bench_refs_on_the_committed_flat_benchmark(flat_db: Path) -> None:
    result = runner.invoke(
        app,
        [
            "bench",
            "refs",
            "--questions",
            str(COMMITTED / "questions.jsonl"),
            "--sql-dir",
            str(COMMITTED / "reference_sql"),
            "--db",
            str(flat_db),
        ],
    )
    assert result.exit_code == 0, result.output
