from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from adsight.cli import app
from adsight.data.build import open_readonly
from adsight.semantic.check import check_model
from adsight.semantic.inspect import InspectError, draft_model
from adsight.semantic.metrics import METRICS, available_metrics, sql_expr
from adsight.semantic.model import SemanticModel, dump_model, load_model
from adsight.semantic.sql import date_col, from_clause, level_key, level_label

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
WEEKS = [date(2026, 1, 5) + timedelta(days=7 * i) for i in range(6)]

runner = CliRunner()


def _flat_rows() -> list[tuple[Any, ...]]:
    rows = []
    for w, week in enumerate(WEEKS):
        for c, (campaign_id, campaign_name) in enumerate((("C1", "Alpha"), ("C2", "Beta"))):
            for g in (1, 2):
                base = 100 + 10 * w + 20 * c + 5 * g
                rows.append(
                    (
                        week,
                        campaign_id,
                        campaign_name,
                        f"{campaign_id}-G{g}",
                        float(base),
                        base * 90,
                        base + 7,
                        base // 20,
                    )
                )
    return rows


@pytest.fixture(scope="module")
def demo_con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(bench_data_dir / "adsight.duckdb")
    yield con
    con.close()


@pytest.fixture(scope="module")
def flat_con(tmp_path_factory: pytest.TempPathFactory) -> Iterator[duckdb.DuckDBPyConnection]:
    path = tmp_path_factory.mktemp("flat") / "flat.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE perf(week_start DATE, campaign_id VARCHAR, campaign_name VARCHAR, "
        "ad_group VARCHAR, cost DOUBLE, impr BIGINT, clicks BIGINT, conversions BIGINT)"
    )
    con.executemany("INSERT INTO perf VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _flat_rows())
    con.close()
    ro = duckdb.connect(str(path), read_only=True)
    yield ro
    ro.close()


@pytest.fixture(scope="module")
def demo_model(demo_con: duckdb.DuckDBPyConnection) -> SemanticModel:
    return draft_model(demo_con, "adsight-demo")[0]


@pytest.fixture(scope="module")
def flat_draft(flat_con: duckdb.DuckDBPyConnection) -> tuple[SemanticModel, list[str]]:
    return draft_model(flat_con, "flat")


# --- model validation ------------------------------------------------------


def _model_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "name": "m",
        "grain": "day",
        "fact": {
            "table": "facts",
            "date": "date",
            "measures": {"spend": "cost", "impressions": "impr", "conversions": "conv"},
        },
        "levels": [
            {"name": "campaign", "table": "campaigns", "key": "campaign_id"},
            {"name": "ad", "table": "ads", "key": "ad_id", "parent_key": "campaign_id"},
        ],
    }
    base.update(overrides)
    return base


def test_valid_model_round_trips(tmp_path: Path) -> None:
    model = SemanticModel.model_validate(_model_dict())
    path = tmp_path / "m.yaml"
    dump_model(model, path)
    assert load_model(path) == model


def test_missing_required_measure_raises() -> None:
    fact = {"table": "facts", "date": "date", "measures": {"spend": "cost", "impressions": "i"}}
    with pytest.raises(ValidationError, match="required measures not mapped: conversions"):
        SemanticModel.model_validate(_model_dict(fact=fact))


def test_duplicate_level_raises() -> None:
    levels = [
        {"name": "campaign", "table": "campaigns", "key": "campaign_id"},
        {"name": "campaign", "table": "facts", "key": "campaign_id"},
    ]
    with pytest.raises(ValidationError, match="duplicate level names: campaign"):
        SemanticModel.model_validate(_model_dict(levels=levels))


def test_own_table_level_without_parent_raises() -> None:
    levels = [
        {"name": "campaign", "table": "campaigns", "key": "campaign_id"},
        {"name": "ad", "table": "ads", "key": "ad_id"},
    ]
    with pytest.raises(ValidationError, match="'ad' has its own table 'ads' but no parent_key"):
        SemanticModel.model_validate(_model_dict(levels=levels))


def test_fact_level_without_parent_is_fine() -> None:
    levels = [
        {"name": "campaign", "table": "facts", "key": "campaign_id"},
        {"name": "ad_set", "table": "facts", "key": "ad_group"},
    ]
    assert SemanticModel.model_validate(_model_dict(levels=levels)).levels[1].parent_key is None


# --- inspect ---------------------------------------------------------------


def test_inspect_demo_matches_committed_yaml(
    demo_model: SemanticModel, demo_con: duckdb.DuckDBPyConnection
) -> None:
    assert demo_model == load_model(DEMO_YAML)
    assert check_model(demo_model, demo_con) == []


def test_inspect_flat(
    flat_draft: tuple[SemanticModel, list[str]], flat_con: duckdb.DuckDBPyConnection
) -> None:
    model, notes = flat_draft
    assert model.fact.table == "perf"
    assert model.fact.date == "week_start"
    assert model.fact.measures == {
        "spend": "cost",
        "impressions": "impr",
        "clicks": "clicks",
        "conversions": "conversions",
    }
    assert model.grain == "week"
    assert [(lv.name, lv.table, lv.key, lv.label) for lv in model.levels] == [
        ("campaign", "perf", "campaign_id", "campaign_name"),
        ("ad_set", "perf", "ad_group", None),
    ]
    assert all(lv.parent_key is None for lv in model.levels)
    assert model.change_log is None
    assert model.conversion_name == "conversion"
    assert model.attributes == []
    assert any("grain week" in n for n in notes)
    assert check_model(model, flat_con) == []


def test_inspect_raises_when_measures_missing(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "bad.duckdb"))
    con.execute("CREATE TABLE t(date DATE, campaign_id VARCHAR, cost DOUBLE)")
    con.execute("INSERT INTO t VALUES (DATE '2026-01-01', 'C1', 1.0)")
    try:
        with pytest.raises(InspectError, match="impressions, conversions"):
            draft_model(con, "bad")
    finally:
        con.close()


# --- metrics and SQL -------------------------------------------------------


def test_available_metrics(demo_model: SemanticModel, flat_draft: tuple[Any, Any]) -> None:
    assert [m.name for m in available_metrics(demo_model)] == list(METRICS)
    assert len(METRICS) == 11
    flat_model: SemanticModel = flat_draft[0]
    flat = {m.name for m in available_metrics(flat_model)}
    assert flat == set(METRICS) - {"link_clicks", "frequency"}
    assert "f.clicks" in sql_expr(flat_model, "cvr")
    assert "f.link_clicks" in sql_expr(demo_model, "cvr")


def test_unmapped_metric_raises(flat_draft: tuple[Any, Any]) -> None:
    with pytest.raises(ValueError, match="needs unmapped measures"):
        sql_expr(flat_draft[0], "frequency")


def _grouped_sql(model: SemanticModel, metric: str) -> str:
    names = [lv.name for lv in model.levels]
    labels = [level_label(model, n) for n in names]
    return (
        f"SELECT {', '.join(labels)}, {sql_expr(model, metric)} AS v\n"
        f"{from_clause(model, names, [])}\n"
        f"GROUP BY {', '.join(labels)}"
    )


@pytest.mark.parametrize("which", ["demo", "flat"])
def test_every_available_metric_runs(
    which: str,
    demo_model: SemanticModel,
    demo_con: duckdb.DuckDBPyConnection,
    flat_draft: tuple[Any, Any],
    flat_con: duckdb.DuckDBPyConnection,
) -> None:
    model, con = (demo_model, demo_con) if which == "demo" else (flat_draft[0], flat_con)
    for metric in available_metrics(model):
        rows = con.execute(_grouped_sql(model, metric.name)).fetchall()
        assert rows, metric.name


def test_demo_cpl_by_campaign_matches_hand_written_sql(
    demo_model: SemanticModel, demo_con: duckdb.DuckDBPyConnection
) -> None:
    generated = demo_con.execute(
        f"SELECT {level_key(demo_model, 'campaign')}, {sql_expr(demo_model, 'cpl')} "
        f"{from_clause(demo_model, ['campaign'], [])} GROUP BY 1 ORDER BY 1"
    ).fetchall()
    hand = demo_con.execute(
        "SELECT campaign_id, SUM(m.spend) / SUM(m.leads) "
        "FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in generated] == [r[0] for r in hand]
    for (_, got), (_, want) in zip(generated, hand, strict=True):
        assert got == pytest.approx(float(want))


def test_from_clause_shapes(demo_model: SemanticModel, flat_draft: tuple[Any, Any]) -> None:
    flat_model: SemanticModel = flat_draft[0]
    assert from_clause(flat_model, ["campaign", "ad_set"], []) == "FROM perf f"
    assert date_col(flat_model) == "f.week_start"
    assert from_clause(demo_model, [], []) == "FROM daily_ad_metrics f"
    assert from_clause(demo_model, ["ad_set"], []) == (
        "FROM daily_ad_metrics f\n"
        "LEFT JOIN ads ad ON ad.ad_id = f.ad_id\n"
        "LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id"
    )
    assert "LEFT JOIN accounts account ON account.account_id = campaign.account_id" in (
        from_clause(demo_model, [], ["vertical"])
    )


def test_attribute_group_by_runs(
    demo_model: SemanticModel, demo_con: duckdb.DuckDBPyConnection
) -> None:
    from adsight.semantic.sql import attribute_col

    rows = demo_con.execute(
        f"SELECT {attribute_col(demo_model, 'vertical')}, {sql_expr(demo_model, 'spend')} "
        f"{from_clause(demo_model, [], ['vertical'])} GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in rows] == ["home_services", "med_spa", "regenerative_clinic"]


# --- check -----------------------------------------------------------------


def test_check_reports_wrong_column(
    demo_model: SemanticModel, demo_con: duckdb.DuckDBPyConnection
) -> None:
    measures = {**demo_model.fact.measures, "spend": "spendd"}
    broken = demo_model.model_copy(
        update={"fact": demo_model.fact.model_copy(update={"measures": measures})}
    )
    problems = check_model(broken, demo_con)
    assert problems == ["measure spend: column daily_ad_metrics.spendd does not exist"]


def test_check_reports_wrong_grain(
    demo_model: SemanticModel, demo_con: duckdb.DuckDBPyConnection
) -> None:
    problems = check_model(demo_model.model_copy(update={"grain": "week"}), demo_con)
    assert len(problems) == 1
    assert problems[0].startswith("grain week")


def test_check_reports_orphan_key(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "orphan.duckdb"))
    try:
        con.execute("CREATE TABLE campaigns(campaign_id VARCHAR, campaign_name VARCHAR)")
        con.execute("INSERT INTO campaigns VALUES ('C1', 'Alpha'), ('C2', 'Beta')")
        con.execute(
            "CREATE TABLE perf(week_start DATE, campaign_id VARCHAR, cost DOUBLE, "
            "impr BIGINT, conversions BIGINT)"
        )
        con.executemany(
            "INSERT INTO perf VALUES (?, ?, 10.0, 100, 1)",
            [(w, c) for w in WEEKS[:3] for c in ("C1", "C2", "C9")],
        )
        model, _ = draft_model(con, "orphan")
        assert model.levels[0].table == "campaigns"
        problems = check_model(model, con)
        assert problems == ["level campaign: 3 fact rows have a campaign_id missing from campaigns"]
    finally:
        con.close()


# --- cli -------------------------------------------------------------------


def _write_csvs(folder: Path) -> list[Path]:
    campaigns = folder / "campaigns.csv"
    campaigns.write_text(
        "campaign_id,campaign_name,objective\nC1,Alpha,leads\nC2,Beta,traffic\n", encoding="utf-8"
    )
    perf = folder / "perf.csv"
    lines = ["week_start,campaign_id,cost,impr,clicks,conversions"]
    for w in WEEKS:
        for c in ("C1", "C2"):
            lines.append(f"{w.isoformat()},{c},12.5,1000,30,2")
    perf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [campaigns, perf]


def test_cli_load_inspect_check(tmp_path: Path) -> None:
    csvs = _write_csvs(tmp_path)
    db = tmp_path / "loaded.duckdb"
    out = tmp_path / "model.yaml"
    loaded = runner.invoke(app, ["load", *map(str, csvs), "--db", str(db)])
    assert loaded.exit_code == 0, loaded.output
    again = runner.invoke(app, ["load", *map(str, csvs), "--db", str(db)])
    assert again.exit_code == 1
    forced = runner.invoke(app, ["load", *map(str, csvs), "--db", str(db), "--force"])
    assert forced.exit_code == 0, forced.output

    inspected = runner.invoke(app, ["inspect", "--db", str(db), "--out", str(out), "--name", "t"])
    assert inspected.exit_code == 0, inspected.output
    model = load_model(out)
    assert model.grain == "week"
    assert model.levels[0].table == "campaigns"
    assert [a.name for a in model.attributes] == ["objective"]

    checked = runner.invoke(app, ["semantic", "check", str(out), "--db", str(db)])
    assert checked.exit_code == 0, checked.output
    assert "OK" in checked.output


def test_cli_check_fails_and_inspect_errors(tmp_path: Path) -> None:
    csvs = _write_csvs(tmp_path)
    db = tmp_path / "loaded.duckdb"
    assert runner.invoke(app, ["load", *map(str, csvs), "--db", str(db)]).exit_code == 0
    bad_model = SemanticModel.model_validate(
        _model_dict(fact={**_model_dict()["fact"], "table": "perf"})
    )
    bad_yaml = tmp_path / "bad.yaml"
    dump_model(bad_model, bad_yaml)
    checked = runner.invoke(app, ["semantic", "check", str(bad_yaml), "--db", str(db)])
    assert checked.exit_code == 1
    assert "PROBLEM" in checked.output

    empty = tmp_path / "empty.csv"
    empty.write_text("week_start,cost\n2026-01-05,1.0\n", encoding="utf-8")
    db2 = tmp_path / "empty.duckdb"
    assert runner.invoke(app, ["load", str(empty), "--db", str(db2)]).exit_code == 0
    failed = runner.invoke(app, ["inspect", "--db", str(db2), "--out", str(tmp_path / "x.yaml")])
    assert failed.exit_code == 1
