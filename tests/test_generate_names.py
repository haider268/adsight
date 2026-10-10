import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from adsight.benchmark.questions import load_questions
from adsight.cli import app
from adsight.data.build import open_readonly
from adsight.generate.context import build_context
from adsight.generate.periods import full_weeks
from adsight.generate.templates import Env, build_env
from adsight.semantic.model import load_model

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"

runner = CliRunner()


@pytest.fixture(scope="module")
def con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = open_readonly(bench_data_dir / "adsight.duckdb")
    yield c
    c.close()


@pytest.fixture(scope="module")
def env(con: duckdb.DuckDBPyConnection) -> Env:
    model = load_model(DEMO_YAML)
    return build_env(model, con, build_context(model, con))


def test_unique_labels_are_unchanged(env: Env) -> None:
    assert env.entity("campaign", "C3") == 'the campaign "Clinic Awareness"'
    assert env.entity("ad_set", "S13") == 'the ad set "Homeowners 35-65"'


def test_duplicate_label_gets_its_parent(env: Env) -> None:
    assert env.label("ad_set", "S06") == env.label("ad_set", "S18") == "Video Viewers"
    assert env.entity("ad_set", "S06") == (
        'the ad set "Video Viewers" in the campaign "Clinic Awareness"'
    )
    assert env.entity("ad_set", "S18") == (
        'the ad set "Video Viewers" in the campaign "Roofing Inspection"'
    )
    ad = next(k for k, lab in env.entities["ad"] if lab == "Video Viewers | Video")
    text = env.entity("ad", ad)
    assert text.startswith('the ad "Video Viewers | Video" in the ad set "Video Viewers" in ')


def test_key_fallback_when_no_parent_separates(env: Env) -> None:
    # make S18 a second "Video Viewers" in the SAME campaign as S06, and clash two accounts
    parent_of = {level: dict(keys) for level, keys in env.parent_of.items()}
    parent_of["ad_set"]["S18"] = dict(parent_of["ad_set"]["S06"])
    labels = {level: dict(names) for level, names in env.labels.items()}
    labels["account"]["A2"] = labels["account"]["A1"]
    entities = {level: list(names.items()) for level, names in labels.items()}
    clashing = replace(env, parent_of=parent_of, labels=labels, entities=entities)
    assert clashing.entity("ad_set", "S06") == 'the ad set "Video Viewers" (S06)'
    assert clashing.entity("ad_set", "S18") == 'the ad set "Video Viewers" (S18)'
    name = labels["account"]["A1"]
    assert clashing.entity("account", "A2") == f'the account "{name}" (A2)'


def test_week_text_states_the_exact_span(env: Env) -> None:
    weeks = {p.start: p.text for p in full_weeks(env.ctx)}
    assert weeks[date(2026, 2, 23)] == (
        "the week of 23 February 2026 (Monday 23 February to Sunday 1 March 2026)"
    )
    assert weeks[date(2026, 3, 23)] == (
        "the week of 23 March 2026 (Monday 23 March to Sunday 29 March 2026)"
    )


def test_demo_questions_never_name_video_viewers_bare() -> None:
    for q in load_questions(DEMO_SET / "questions.jsonl"):
        for m in re.finditer(r'"Video Viewers"', q.question):
            assert q.question[m.end() :].startswith(" in the campaign "), q.id
        for m in re.finditer(r'"Video Viewers \| \w+"', q.question):
            assert q.question[m.end() :].startswith(' in the ad set "Video Viewers" in '), q.id
    tr09 = next(q for q in load_questions(DEMO_SET / "questions.jsonl") if q.id == "TR09")
    assert 'the ad set "Video Viewers" in the campaign "Clinic Awareness"' in tr09.question


def test_bench_refs_on_the_committed_demo(bench_data_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "bench",
            "refs",
            "--questions",
            str(DEMO_SET / "questions.jsonl"),
            "--sql-dir",
            str(DEMO_SET / "reference_sql"),
            "--db",
            str(bench_data_dir / "adsight.duckdb"),
        ],
    )
    assert result.exit_code == 0, result.output
