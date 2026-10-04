import random
from collections import Counter
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from adsight.benchmark.questions import Question, load_questions
from adsight.benchmark.refs import compute_expected
from adsight.benchmark.score import reference_answer, score
from adsight.cli import app
from adsight.data.build import open_readonly
from adsight.generate.context import build_context
from adsight.generate.generator import GenerateReport, generate_benchmark
from adsight.generate.templates import (
    PROBES,
    ambiguous_phrasings,
    available_probes,
    build_env,
)
from adsight.semantic.inspect import draft_model
from adsight.semantic.model import SemanticModel, dump_model, load_model

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
ARTEFACTS = ("questions.jsonl", "context.json", "lint_report.json")

runner = CliRunner()


# --- fixtures ----------------------------------------------------------------


def _flat_rows() -> list[tuple[object, ...]]:
    rng = random.Random(1)
    rows: list[tuple[object, ...]] = []
    for c, name in enumerate(("Alpha", "Beta", "Gamma"), start=1):
        for g in (1, 2):
            for w in range(30):
                impr = rng.randint(15000, 30000)
                clicks = round(impr * rng.uniform(0.01, 0.02))
                conversions = round(clicks * rng.uniform(0.03, 0.08))
                rows.append(
                    (
                        date(2026, 1, 5) + timedelta(days=7 * w),
                        f"C{c}",
                        name,
                        f"C{c}-G{g}",
                        rng.uniform(200, 400),
                        impr,
                        clicks,
                        conversions,
                    )
                )
    return rows


def _make_flat(path: Path, extra_revenue: bool = False) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE perf(week_start DATE, campaign_id VARCHAR, campaign_name VARCHAR, "
            "ad_group VARCHAR, cost DOUBLE, impr BIGINT, clicks BIGINT, conversions BIGINT)"
        )
        con.executemany("INSERT INTO perf VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _flat_rows())
        if extra_revenue:
            con.execute("ALTER TABLE perf ADD COLUMN revenue DOUBLE")
    finally:
        con.close()


@pytest.fixture(scope="module")
def demo_db(bench_data_dir: Path) -> Path:
    return bench_data_dir / "adsight.duckdb"


@pytest.fixture(scope="module")
def demo_con(demo_db: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(demo_db)
    yield con
    con.close()


@pytest.fixture(scope="module")
def demo_model() -> SemanticModel:
    return load_model(DEMO_YAML)


@pytest.fixture(scope="module")
def generated(
    tmp_path_factory: pytest.TempPathFactory,
    demo_model: SemanticModel,
    demo_con: duckdb.DuckDBPyConnection,
) -> tuple[Path, GenerateReport]:
    out = tmp_path_factory.mktemp("gen7")
    return out, generate_benchmark(demo_model, demo_con, 7, out)


@pytest.fixture(scope="module")
def flat_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("flatgen") / "flat.duckdb"
    _make_flat(path)
    return path


@pytest.fixture(scope="module")
def flat_con(flat_db: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(flat_db)
    yield con
    con.close()


@pytest.fixture(scope="module")
def flat_model(flat_con: duckdb.DuckDBPyConnection) -> SemanticModel:
    return draft_model(flat_con, "flat")[0]


@pytest.fixture(scope="module")
def flat_generated(
    tmp_path_factory: pytest.TempPathFactory,
    flat_model: SemanticModel,
    flat_con: duckdb.DuckDBPyConnection,
) -> tuple[Path, GenerateReport]:
    out = tmp_path_factory.mktemp("genflat")
    return out, generate_benchmark(flat_model, flat_con, 7, out)


# --- the generated demo benchmark ------------------------------------------------


def test_demo_shape(generated: tuple[Path, GenerateReport]) -> None:
    out, report = generated
    questions = load_questions(out / "questions.jsonl")
    assert len(questions) == 80
    assert Counter(q.category for q in questions) == {
        "lookup": 15,
        "aggregation": 15,
        "comparison": 15,
        "trend": 15,
        "ambiguous": 10,
        "unanswerable": 10,
    }
    assert report.per_category["root_cause"] == {"target": 0, "selected": 0}
    prefixes = [q.id[:2] for q in questions]
    assert prefixes == sorted(prefixes, key=["LK", "AG", "CP", "TR", "AM", "UN"].index)
    assert all(q.template for q in questions)
    for q in questions:
        if q.reference_sql is not None:
            assert (out / "reference_sql" / q.reference_sql).read_text().endswith(";\n")
    assert len(list((out / "reference_sql").glob("*.sql"))) == 60


def test_demo_expected_is_current_and_reference_answers_pass(
    generated: tuple[Path, GenerateReport], demo_con: duckdb.DuckDBPyConnection
) -> None:
    out, _ = generated
    for q in load_questions(out / "questions.jsonl"):
        if q.reference_sql is not None:
            assert compute_expected(q, out / "reference_sql", demo_con) == q.expected, q.id
        result = score(q, reference_answer(q, {}))
        assert result.passed, (q.id, result.reason)


def test_demo_trend_directions(generated: tuple[Path, GenerateReport]) -> None:
    out, report = generated
    trends = [q for q in load_questions(out / "questions.jsonl") if q.category == "trend"]
    counts = Counter(q.expected.direction for q in trends if q.expected is not None)
    assert all(counts[d] >= 3 for d in ("up", "down", "flat")), counts
    assert report.trend_directions == dict(counts)


def test_template_share_cap(generated: tuple[Path, GenerateReport]) -> None:
    out, _ = generated
    questions = load_questions(out / "questions.jsonl")
    for category in ("lookup", "aggregation", "comparison"):
        per_template = Counter(q.template for q in questions if q.category == category)
        assert max(per_template.values()) <= 6, (category, per_template)


def test_generation_is_deterministic(
    generated: tuple[Path, GenerateReport],
    demo_model: SemanticModel,
    demo_con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
) -> None:
    out, _ = generated
    again, other = tmp_path / "again", tmp_path / "seed8"
    generate_benchmark(demo_model, demo_con, 7, again)
    generate_benchmark(demo_model, demo_con, 8, other)
    for name in ARTEFACTS:
        assert (again / name).read_bytes() == (out / name).read_bytes(), name
    sql_a = sorted(p.name for p in (out / "reference_sql").glob("*.sql"))
    assert sql_a == sorted(p.name for p in (again / "reference_sql").glob("*.sql"))
    for name in sql_a:
        assert (again / "reference_sql" / name).read_bytes() == (
            out / "reference_sql" / name
        ).read_bytes()
    assert (other / "questions.jsonl").read_bytes() != (out / "questions.jsonl").read_bytes()


def test_demo_lint_report(generated: tuple[Path, GenerateReport]) -> None:
    import json

    out, report = generated
    data = json.loads((out / "lint_report.json").read_text(encoding="utf-8"))
    assert data["accepted"] >= 80
    assert data["candidates_evaluated"] == data["accepted"] + sum(data["rejected_by_rule"].values())
    assert data["shortfalls"] == report.shortfalls == []
    for examples in data["rejected_examples"].values():
        assert 1 <= len(examples) <= 3
        assert set(examples[0]) == {"template", "params", "question", "reason"}
    context = json.loads((out / "context.json").read_text(encoding="utf-8"))
    assert context["data_end"] == "2026-06-29" and context["today"] == "2026-06-30"


# --- the flat weekly database ------------------------------------------------------


def test_flat_generation(
    flat_generated: tuple[Path, GenerateReport], flat_con: duckdb.DuckDBPyConnection
) -> None:
    out, report = flat_generated
    questions = load_questions(out / "questions.jsonl")
    assert questions
    assert isinstance(report.shortfalls, list)
    for q in questions:
        assert q.template not in ("A_WEEKDAY", "L_DAILY")
        assert q.params.get("metric") not in ("frequency", "link_clicks")
        if q.reference_sql is not None:
            sql = (out / "reference_sql" / q.reference_sql).read_text(encoding="utf-8")
            assert flat_con.execute(sql).fetchall(), q.id
            assert "JOIN" not in sql


def test_flat_context_is_weekly(
    flat_model: SemanticModel, flat_con: duckdb.DuckDBPyConnection
) -> None:
    ctx = build_context(flat_model, flat_con)
    assert ctx.grain == "week"
    assert ctx.data_start == date(2026, 1, 5)
    assert ctx.data_end == date(2026, 1, 5) + timedelta(days=7 * 29 + 6)
    assert ctx.conversion_name == "conversion"


def test_flat_strict_exit_matches_shortfalls(
    flat_db: Path,
    flat_model: SemanticModel,
    flat_generated: tuple[Path, GenerateReport],
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "flat.yaml"
    dump_model(flat_model, yaml_path)
    args = ["bench", "generate", "--semantic", str(yaml_path), "--db", str(flat_db)]
    plain = runner.invoke(app, [*args, "--out", str(tmp_path / "a")])
    assert plain.exit_code == 0, plain.output
    strict = runner.invoke(app, [*args, "--out", str(tmp_path / "b"), "--strict"])
    assert strict.exit_code == (1 if flat_generated[1].shortfalls else 0), strict.output


# --- probes and phrasings ----------------------------------------------------------


def test_demo_probes_match_no_column(
    generated: tuple[Path, GenerateReport],
    demo_model: SemanticModel,
    demo_con: duckdb.DuckDBPyConnection,
) -> None:
    out, _ = generated
    env = build_env(demo_model, demo_con, build_context(demo_model, demo_con))
    words = {p.concept: p.words for p in PROBES}
    for q in load_questions(out / "questions.jsonl"):
        if q.category == "unanswerable":
            probe_words = words[q.params["probe"]]
            assert not any(w in col for w in probe_words for col in env.columns), q.params


def test_revenue_column_removes_revenue_probes(tmp_path: Path) -> None:
    path = tmp_path / "flat_rev.duckdb"
    _make_flat(path, extra_revenue=True)
    con = open_readonly(path)
    try:
        model = draft_model(con, "flat")[0]
        env = build_env(model, con, build_context(model, con))
        concepts = {p.concept for p in available_probes(env)}
    finally:
        con.close()
    assert "revenue" not in concepts and "profit" not in concepts
    assert {"market", "before_start"} <= concepts


def test_change_phrasing_needs_change_log(
    demo_model: SemanticModel,
    demo_con: duckdb.DuckDBPyConnection,
    flat_model: SemanticModel,
    flat_con: duckdb.DuckDBPyConnection,
) -> None:
    demo_env = build_env(demo_model, demo_con, build_context(demo_model, demo_con))
    flat_env = build_env(flat_model, flat_con, build_context(flat_model, flat_con))
    assert "change_work" in {k for k, _ in ambiguous_phrasings(demo_env)}
    assert "change_work" not in {k for k, _ in ambiguous_phrasings(flat_env)}


# --- cli -------------------------------------------------------------------------


def test_cli_generate_then_refs(demo_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "bench"
    result = runner.invoke(
        app,
        [
            "bench",
            "generate",
            "--semantic",
            str(DEMO_YAML),
            "--db",
            str(demo_db),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    for name in ARTEFACTS:
        assert (out / name).is_file(), name
    assert (out / "reference_sql").is_dir()
    refs = runner.invoke(
        app,
        [
            "bench",
            "refs",
            "--questions",
            str(out / "questions.jsonl"),
            "--sql-dir",
            str(out / "reference_sql"),
            "--data-dir",
            str(demo_db.parent),
        ],
    )
    assert refs.exit_code == 0, refs.output


def test_question_keeps_template_and_params(generated: tuple[Path, GenerateReport]) -> None:
    out, _ = generated
    first = load_questions(out / "questions.jsonl")[0]
    assert isinstance(first, Question)
    assert first.template is not None and first.params
