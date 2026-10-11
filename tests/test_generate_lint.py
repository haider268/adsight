from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from adsight.benchmark.questions import Question, load_questions
from adsight.benchmark.refs import compute_expected
from adsight.benchmark.score import matches
from adsight.data.build import open_readonly
from adsight.generate.context import DatasetContext, build_context
from adsight.generate.lint import distinct_problem, lint
from adsight.generate.periods import all_periods, day_text, full_months, full_quarters
from adsight.generate.templates import Candidate, Env, build_candidate, build_env
from adsight.semantic.model import SemanticModel, load_model

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"


@pytest.fixture(scope="module")
def demo_con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(bench_data_dir / "adsight.duckdb")
    yield con
    con.close()


@pytest.fixture(scope="module")
def demo_model() -> SemanticModel:
    return load_model(DEMO_YAML)


@pytest.fixture(scope="module")
def demo_ctx(demo_model: SemanticModel, demo_con: duckdb.DuckDBPyConnection) -> DatasetContext:
    return build_context(demo_model, demo_con)


@pytest.fixture(scope="module")
def demo_env(
    demo_model: SemanticModel, demo_con: duckdb.DuckDBPyConnection, demo_ctx: DatasetContext
) -> Env:
    return build_env(demo_model, demo_con, demo_ctx)


@pytest.fixture(scope="module")
def gold(questions_path: Path) -> dict[str, Question]:
    return {q.id: q for q in load_questions(questions_path)}


def _lint(env: Env, con: duckdb.DuckDBPyConnection, template: str, params: dict[str, str]) -> Any:
    cand = build_candidate(env, template, params)
    return cand, lint(cand, con, env.model, env.ctx)


def _rows(con: duckdb.DuckDBPyConnection, cand: Candidate) -> list[tuple[Any, ...]]:
    assert cand.sql is not None
    return con.execute(cand.sql).fetchall()


# --- context and periods -----------------------------------------------------


def test_demo_context(demo_ctx: DatasetContext) -> None:
    assert demo_ctx.data_start == date(2026, 1, 1)
    assert demo_ctx.data_end == date(2026, 6, 29)
    assert demo_ctx.today == date(2026, 6, 30)
    assert demo_ctx.conversion_name == "lead"
    assert demo_ctx.conversion_plural == "leads"
    assert demo_ctx.grain == "day"


def test_demo_periods(demo_ctx: DatasetContext) -> None:
    months = full_months(demo_ctx)
    assert [p.key for p in months] == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    assert months[2].text == "March 2026"
    assert [(q.key, q.text) for q in full_quarters(demo_ctx)] == [
        ("2026-Q1", "the first quarter of 2026 (January to March 2026)")
    ]
    periods = {p.key: p for p in all_periods(demo_ctx)}
    assert (periods["last30"].start, periods["last30"].end) == (
        date(2026, 5, 31),
        date(2026, 6, 29),
    )
    assert periods["last30"].text == "the last 30 days (31 May to 29 June 2026)"
    assert periods["whole"].text == "the whole period"
    weeks = [p for p in periods.values() if p.kind == "week"]
    assert weeks[0].start == date(2026, 1, 5) and weeks[-1].end == date(2026, 6, 28)
    assert all(w.start.weekday() == 0 and w.end - w.start == timedelta(days=6) for w in weeks)
    assert periods["2026-W11"].text == (
        "the week of 9 March 2026 (Monday 9 March to Sunday 15 March 2026)"
    )
    assert len(weeks) == 25
    for p in periods.values():
        assert " 0" not in p.text
    assert day_text(date(2026, 3, 9)) == "9 March 2026"


# --- the linter: known bad and known good --------------------------------------


def test_bad_a_ctr_ranking_has_no_margin(
    demo_env: Env, demo_con: duckdb.DuckDBPyConnection
) -> None:
    params = {"metric": "ctr", "level": "ad", "direction": "highest", "period": "2026-05"}
    _, result = _lint(demo_env, demo_con, "A_RANK", params)
    assert result.rule == "R3_MARGIN", result


def test_bad_b_frequency_ranking_has_no_margin(
    demo_env: Env, demo_con: duckdb.DuckDBPyConnection
) -> None:
    params = {"metric": "frequency", "level": "ad_set", "direction": "highest", "period": "last30"}
    cand, result = _lint(demo_env, demo_con, "A_RANK", params)
    top_two = [(r[0], r[2]) for r in _rows(demo_con, cand)[:2]]
    assert result.rule == "R3_MARGIN", (result, top_two)


def test_bad_c_flat_ish_trend_rejected(demo_env: Env, demo_con: duckdb.DuckDBPyConnection) -> None:
    params = {
        "metric": "spend",
        "level": "ad_set",
        "entity": "S07",
        "start": "2026-03-02",
        "weeks": "4",
    }
    cand, result = _lint(demo_env, demo_con, "T_WEEKLY", params)
    assert "Women 30-55 Broad" in cand.question
    assert result.rule == "R5_TREND", result


def test_bad_d_close_values_rejected_by_r6() -> None:
    result = distinct_problem({"S04": 2.584, "S10": 2.598, "S16": 2.591}, "number")
    assert result.rule == "R6_DISTINCT"


def test_good_most_leads_ad_april(demo_env: Env, demo_con: duckdb.DuckDBPyConnection) -> None:
    params = {"metric": "conversions", "level": "ad", "direction": "highest", "period": "2026-04"}
    cand, result = _lint(demo_env, demo_con, "A_RANK", params)
    assert result.ok, result
    assert _rows(demo_con, cand)[0][0] == "AD020"


def test_good_ad001_weekly_ctr_down(demo_env: Env, demo_con: duckdb.DuckDBPyConnection) -> None:
    params = {
        "metric": "ctr",
        "level": "ad",
        "entity": "AD001",
        "start": "2026-05-04",
        "weeks": "8",
    }
    cand, result = _lint(demo_env, demo_con, "T_WEEKLY", params)
    assert 'the ad "Knee 45-65 Broad | Video"' in cand.question
    assert result.ok, result
    rows = _rows(demo_con, cand)
    assert len(rows) == 8 and rows[-1][1] < rows[0][1]


def test_good_a3_monthly_cpl_up(demo_env: Env, demo_con: duckdb.DuckDBPyConnection) -> None:
    params = {
        "metric": "cpl",
        "level": "account",
        "entity": "A3",
        "first": "2026-01",
        "last": "2026-05",
    }
    cand, result = _lint(demo_env, demo_con, "T_MONTHLY", params)
    assert "Northside Home Services" in cand.question
    assert result.ok, result
    rows = _rows(demo_con, cand)
    assert rows[-1][1] > rows[0][1]


def test_duplicate_rejected_by_r8(demo_env: Env, demo_con: duckdb.DuckDBPyConnection) -> None:
    params = {"metric": "spend", "period": "2026-03"}
    cand = build_candidate(demo_env, "L_ALL", params)
    seen = {("L_ALL", tuple(sorted(params.items())))}
    assert lint(cand, demo_con, demo_env.model, demo_env.ctx, seen=seen).rule == "R8_DUPLICATE"


# --- gold agreement: the metric library against hand-written SQL -------------------


def _expected(
    env: Env, con: duckdb.DuckDBPyConnection, template: str, params: dict[str, str]
) -> Any:
    cand = build_candidate(env, template, params)
    assert cand.sql is not None
    q = Question.model_validate(
        {
            "id": {"lookup": "LK99", "aggregation": "AG99", "comparison": "CP99", "trend": "TR99"}[
                cand.category
            ],
            "category": cand.category,
            "question": cand.question,
            "scoring": cand.scoring,
            "reference_sql": "x.sql",
        }
    )
    cursor = con.execute(cand.sql)
    columns = [d[0] for d in cursor.description or []]
    from adsight.benchmark.refs import expected_from_rows

    return expected_from_rows(q.scoring, columns, cursor.fetchall())


def _single(expected: Any) -> float:
    (value,) = expected.values.values()
    return float(value)


@pytest.mark.parametrize(
    ("template", "params", "gold_id", "unit"),
    [
        (
            "L_TOTAL",
            {"metric": "spend", "level": "account", "entity": "A2", "period": "2026-03"},
            "LK01",
            "usd",
        ),
        (
            "L_RATIO",
            {"metric": "cpl", "level": "account", "entity": "A3", "period": "2026-04"},
            "LK04",
            "usd",
        ),
        (
            "L_RATIO",
            {"metric": "ctr", "level": "campaign", "entity": "C7", "period": "2026-01"},
            "LK06",
            "ratio",
        ),
    ],
)
def test_gold_lookups_agree(
    demo_env: Env,
    demo_con: duckdb.DuckDBPyConnection,
    gold: dict[str, Question],
    template: str,
    params: dict[str, str],
    gold_id: str,
    unit: str,
) -> None:
    got = _single(_expected(demo_env, demo_con, template, params))
    want = gold[gold_id].expected
    assert want is not None
    assert matches(_single(want), got, unit)  # type: ignore[arg-type]


def test_gold_rankings_agree(
    demo_env: Env, demo_con: duckdb.DuckDBPyConnection, gold: dict[str, Question]
) -> None:
    ag01 = _expected(
        demo_env,
        demo_con,
        "A_RANK",
        {"metric": "cpl", "level": "campaign", "direction": "lowest", "period": "last30"},
    )
    assert ag01.entity_id == "C5" == gold["AG01"].expected.entity_id  # type: ignore[union-attr]
    ag13 = _expected(
        demo_env,
        demo_con,
        "A_RANK",
        {"metric": "cvr", "level": "account", "direction": "highest", "period": "2026-Q1"},
    )
    assert ag13.entity_id == "A3" == gold["AG13"].expected.entity_id  # type: ignore[union-attr]


def test_gold_comparison_and_trend_agree(
    demo_env: Env, demo_con: duckdb.DuckDBPyConnection, gold: dict[str, Question]
) -> None:
    cp = _expected(
        demo_env, demo_con, "C_LEVEL", {"metric": "cpl", "level": "account", "period": "2026-05"}
    )
    gold_cp = gold["CP02"].expected
    assert gold_cp is not None
    names = {
        "A1": "Restore Regenerative Clinic",
        "A2": "Lumen Med Spa",
        "A3": "Northside Home Services",
    }
    for account, want in gold_cp.values.items():
        assert matches(want, cp.values[names[account]], "usd"), account

    tr = _expected(
        demo_env,
        demo_con,
        "T_MONTHLY",
        {
            "metric": "cpl",
            "level": "account",
            "entity": "A3",
            "first": "2026-01",
            "last": "2026-05",
        },
    )
    gold_tr = gold["TR04"].expected
    assert gold_tr is not None
    assert tr.direction == gold_tr.direction == "up"
    for end in ("first", "last"):
        assert matches(gold_tr.values[end], tr.values[end], "usd"), end


def test_compute_expected_reads_generated_sql(
    demo_env: Env, demo_con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    cand = build_candidate(demo_env, "L_ALL", {"metric": "spend", "period": "whole"})
    assert cand.sql is not None and cand.sql.endswith(";\n")
    (tmp_path / "LK99.sql").write_text(cand.sql, encoding="utf-8")
    q = Question.model_validate(
        {
            "id": "LK99",
            "category": "lookup",
            "question": cand.question,
            "scoring": cand.scoring,
            "reference_sql": "LK99.sql",
        }
    )
    assert matches(109460.88, compute_expected(q, tmp_path, demo_con).values["spend"], "usd")


def test_missing_member_rejected_by_r7(demo_env: Env, demo_con: duckdb.DuckDBPyConnection) -> None:
    # AD019 is paused from 2026-03-16, so only 2 of S07's 3 ads have May data
    params = {
        "level": "ad",
        "parent_level": "ad_set",
        "parent_entity": "S07",
        "metric": "spend",
        "period": "2026-05",
    }
    cand, result = _lint(demo_env, demo_con, "C_LEVEL", params)
    assert len(_rows(demo_con, cand)) == 2
    assert result.rule == "R7_COVERAGE", result
    assert "2 of 3" in result.reason
