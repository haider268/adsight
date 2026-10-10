import re
from collections.abc import Iterator
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from adsight.benchmark.answer import AgentAnswer
from adsight.benchmark.questions import (
    Question,
    RootCauseScoring,
    effective_questions,
    load_questions,
    save_questions,
)
from adsight.benchmark.score import score
from adsight.cli import app
from adsight.data.build import open_readonly
from adsight.generate.causes import Detection, detect_causes
from adsight.generate.context import build_context
from adsight.generate.generator import GenerateReport, generate_benchmark
from adsight.generate.templates import Env, build_env
from adsight.semantic.model import SemanticModel, load_model

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
ROUTINE = {
    "S02": "Knee Interest Stack",
    "S08": "Beauty Interests",
    "S14": "New Movers",
    "S04": "Back Pain Retargeting",
    "S10": "Retargeting Site",
    "S16": "Retargeting Quotes",
}
DECOY_ANGLES = ("RC_DECOY", "RC_TRACK_DECOY")
INCIDENT_MATCH_DAYS = 7

runner = CliRunner()


@pytest.fixture(scope="module")
def demo_con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(bench_data_dir / "adsight.duckdb")
    yield con
    con.close()


@pytest.fixture(scope="module")
def demo_env(demo_con: duckdb.DuckDBPyConnection) -> Env:
    model = load_model(DEMO_YAML)
    return build_env(model, demo_con, build_context(model, demo_con))


@pytest.fixture(scope="module")
def detection(demo_env: Env, demo_con: duckdb.DuckDBPyConnection) -> Detection:
    return detect_causes(demo_env, demo_con)


@pytest.fixture(scope="module")
def generated(
    tmp_path_factory: pytest.TempPathFactory, demo_con: duckdb.DuckDBPyConnection
) -> tuple[Path, GenerateReport]:
    out = tmp_path_factory.mktemp("gen-rc")
    return out, generate_benchmark(load_model(DEMO_YAML), demo_con, 7, out)


@pytest.fixture(scope="module")
def rc_questions(generated: tuple[Path, GenerateReport]) -> list[Question]:
    out, _ = generated
    return [q for q in load_questions(out / "questions.jsonl") if q.category == "root_cause"]


def _summary(q: Question) -> str:
    assert isinstance(q.scoring, RootCauseScoring)
    assert q.scoring.reference_summary is not None
    return q.scoring.reference_summary


def _answer(summary: str) -> AgentAnswer:
    return AgentAnswer(kind="answer", summary=summary)


# --- detection on the demo ----------------------------------------------------------


def test_logged_causes(detection: Detection) -> None:
    logged = {c.entity_key: c for c in detection.of_kind("logged")}
    assert set(logged) == {"AD019", "S13", "S09"}
    ad019, s13, s09 = logged["AD019"].evidence, logged["S13"].evidence, logged["S09"].evidence
    assert logged["AD019"].scope_key == "S07"
    assert logged["AD019"].start == date(2026, 3, 16)
    assert (round(float(ad019["cpl_before"]), 2), round(float(ad019["cpl_after"]), 2)) == (
        9.90,
        15.20,
    )
    assert round(float(ad019["paused_share"]) * 100, 1) == 44.7
    assert ad019["parent_key"] == "C4"
    assert (
        round(float(ad019["parent_cpl_before"]), 2),
        round(float(ad019["parent_cpl_after"]), 2),
    ) == (11.14, 13.45)
    assert (s13["old"], s13["new"]) == ("55.00", "110.00")
    assert (round(float(s13["cpl_before"]), 2), round(float(s13["cpl_after"]), 2)) == (12.07, 15.81)
    assert 0.94 <= float(s13["spend_jump"]) <= 1.00  # about +97%
    assert (
        round(float(s13["parent_cpl_before"]), 2),
        round(float(s13["parent_cpl_after"]), 2),
    ) == (11.57, 16.28)
    assert s09["change_type"] == "audience_edit"
    assert (round(float(s09["cpl_before"]), 2), round(float(s09["cpl_after"]), 2)) == (12.85, 23.93)
    assert (
        round(float(s09["parent_cpl_before"]), 2),
        round(float(s09["parent_cpl_after"]), 2),
    ) == (13.52, 23.30)
    assert all(not c.needs_review for c in logged.values())


def test_routine_changes_are_not_material(detection: Detection) -> None:
    routine = [c for c in detection.changes if c.change_type == "budget_change" and not c.material]
    assert sorted(c.key for c in routine) == sorted(ROUTINE)


def test_tracking_fatigue_calendar_noise(detection: Detection) -> None:
    tracking = detection.of_kind("tracking")
    assert [(c.entity_key, c.start, c.end) for c in tracking] == [
        ("A3", date(2026, 6, 8), date(2026, 6, 9))
    ]
    fatigue = detection.of_kind("fatigue")
    assert [(c.entity_key, c.start) for c in fatigue] == [("AD001", date(2026, 5, 18))]
    calendar = detection.of_kind("calendar")
    assert ("C4", date(2026, 1, 1)) in {(c.entity_key, c.start) for c in calendar}
    assert len(detection.of_kind("noise")) >= 3
    assert all(c.needs_review for c in tracking + fatigue + detection.of_kind("noise"))
    assert not any(c.needs_review for c in calendar)


# --- generated root-cause questions -------------------------------------------------


def test_twenty_root_cause_questions(
    rc_questions: list[Question], generated: tuple[Path, GenerateReport]
) -> None:
    _, report = generated
    assert len(rc_questions) == 20
    assert report.per_category["root_cause"] == {"target": 20, "selected": 20}
    keys = [(q.params["cause_id"], q.params["angle"]) for q in rc_questions]
    assert len(keys) == len(set(keys))
    assert sum(1 for q in rc_questions if q.params["kind"] == "fatigue") <= 3
    assert report.root_cause["pending_review"] == sum(
        1 for q in rc_questions if q.review == "pending"
    )
    for q in rc_questions:
        assert q.review == (
            "pending" if q.params["kind"] in ("tracking", "fatigue", "noise") else None
        )


def test_routine_changes_never_named_as_cause(rc_questions: list[Question]) -> None:
    for q in rc_questions:
        assert not any(key in q.params["cause_id"] for key in ROUTINE), q.id
        text = q.question + " " + _summary(q)
        named = [k for k, label in ROUTINE.items() if re.search(rf"\b{k}\b", text) or label in text]
        if q.template not in DECOY_ANGLES:
            assert not named, (q.id, named)
        else:
            assert not any(k in _summary(q) or ROUTINE[k] in _summary(q) for k in named), q.id


def _incident_scope(incident: dict[str, Any]) -> str:
    return str(
        incident["ad_id"]
        or incident["ad_set_id"]
        or incident["campaign_id"]
        or incident["account_id"]
    )


def _days_from_incident(incident: dict[str, Any], day: date) -> int:
    """Distance from `day` to the incident's active window [start, end or open]."""
    start = date.fromisoformat(incident["start_date"])
    end = date.fromisoformat(incident["end_date"]) if incident["end_date"] else date.max
    if day < start:
        return (start - day).days
    return 0 if day <= end else (day - end).days


def _matching(rc_questions: list[Question], incident: dict[str, Any]) -> list[Question]:
    key = _incident_scope(incident)
    return [
        q
        for q in rc_questions
        if q.params["entity"] == key
        and _days_from_incident(incident, date.fromisoformat(q.params["date"]))
        <= INCIDENT_MATCH_DAYS
    ]


def test_gold_incidents_are_covered(
    rc_questions: list[Question], bench_manifest: dict[str, Any], questions_path: Path
) -> None:
    gold = [q for q in load_questions(questions_path) if q.category == "root_cause"]
    report: dict[str, list[str]] = {}
    for incident in bench_manifest["incidents"]:
        iid = incident["incident_id"]
        matches = _matching(rc_questions, incident)
        assert matches, iid
        summary = _summary(matches[0])
        singles = [
            g
            for g in gold
            if isinstance(g.scoring, RootCauseScoring) and g.scoring.incident_ids == [iid]
        ]
        passed = [g.id for g in singles if score(g, _answer(summary)).passed]
        report[iid] = passed
        assert passed, (iid, [g.id for g in singles])
    # documented: RC06 asks for diminishing returns, which the generated summary does not give
    assert "RC06" not in report["I2"]


def test_summaries_fail_other_kinds(rc_questions: list[Question]) -> None:
    for a, b in combinations(rc_questions, 2):
        if a.params["kind"] == b.params["kind"]:
            continue
        assert not score(a, _answer(_summary(b))).passed, (a.id, b.id)
        assert not score(b, _answer(_summary(a))).passed, (b.id, a.id)


def test_reference_answers_pass(rc_questions: list[Question]) -> None:
    for q in rc_questions:
        result = score(q, _answer(_summary(q)))
        assert result.passed, (q.id, result.reason)


# --- review -------------------------------------------------------------------------


def test_effective_questions_exclude_pending(generated: tuple[Path, GenerateReport]) -> None:
    out, _ = generated
    questions = load_questions(out / "questions.jsonl")
    pending = [q for q in questions if q.review == "pending"]
    assert pending
    assert {q.params["kind"] for q in pending} == {"tracking", "fatigue", "noise"}
    effective = effective_questions(questions)
    assert len(effective) == len(questions) - len(pending)
    approved = [q.model_copy(update={"review": "approved"}) for q in pending]
    assert len(effective_questions(approved)) == len(approved)


def test_review_command_flow(generated: tuple[Path, GenerateReport], tmp_path: Path) -> None:
    out, _ = generated
    folder = tmp_path / "copy"
    folder.mkdir()
    (folder / "questions.jsonl").write_bytes((out / "questions.jsonl").read_bytes())
    pending = [q.id for q in load_questions(folder / "questions.jsonl") if q.review == "pending"]
    listing = runner.invoke(app, ["bench", "review", str(folder)])
    assert listing.exit_code == 0, listing.output
    assert pending[0] in listing.output
    decided = runner.invoke(
        app, ["bench", "review", str(folder), "--approve", pending[0], "--reject", pending[1]]
    )
    assert decided.exit_code == 0, decided.output
    states = {q.id: q.review for q in load_questions(folder / "questions.jsonl")}
    assert states[pending[0]] == "approved" and states[pending[1]] == "rejected"
    again = runner.invoke(app, ["bench", "review", str(folder), "--approve", pending[0]])
    assert again.exit_code == 1
    unknown = runner.invoke(app, ["bench", "review", str(folder), "--approve", "RC99"])
    assert unknown.exit_code == 1


def test_reviews_survive_regeneration(
    generated: tuple[Path, GenerateReport], demo_con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    out, _ = generated
    folder = tmp_path / "regen"
    folder.mkdir()
    questions = load_questions(out / "questions.jsonl")
    pending = [q.id for q in questions if q.review == "pending"]
    kept, edited = pending[0], pending[1]
    decided = [
        q.model_copy(update={"review": "approved"})
        if q.id == kept
        else q.model_copy(update={"review": "approved", "question": q.question + " (old)"})
        if q.id == edited
        else q
        for q in questions
    ]
    save_questions(decided, folder / "questions.jsonl")
    report = generate_benchmark(load_model(DEMO_YAML), demo_con, 7, folder)
    states = {q.id: q.review for q in load_questions(folder / "questions.jsonl")}
    assert states[kept] == "approved"
    assert states[edited] == "pending"
    assert report.root_cause["approved"] == 1
    assert report.root_cause["rejected"] == 0
    assert report.root_cause["pending_review"] == len(pending) - 1


def test_committed_demo_regenerates_byte_identical(
    demo_con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    committed = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "questions.jsonl").write_bytes((committed / "questions.jsonl").read_bytes())
    generate_benchmark(load_model(DEMO_YAML), demo_con, 7, folder)
    for name in ("questions.jsonl", "lint_report.json", "context.json"):
        assert (folder / name).read_bytes() == (committed / name).read_bytes(), name
    for sql in (committed / "reference_sql").glob("*.sql"):
        assert (folder / "reference_sql" / sql.name).read_bytes() == sql.read_bytes(), sql.name


# --- a dataset with no change log and no reach ---------------------------------------


def test_flat_weekly_root_causes(tmp_path: Path, make_flat_db: Any) -> None:
    path = tmp_path / "flat.duckdb"
    make_flat_db(path)
    con = open_readonly(path)
    try:
        from adsight.semantic.inspect import draft_model

        model: SemanticModel = draft_model(con, "flat")[0]
        env = build_env(model, con, build_context(model, con))
        det = detect_causes(env, con)
        assert (
            not det.of_kind("logged") and not det.of_kind("fatigue") and not det.of_kind("calendar")
        )
        report = generate_benchmark(model, con, 7, tmp_path / "out")
    finally:
        con.close()
    assert report.per_category["root_cause"]["selected"] < 20
    assert any(s.startswith("root_cause:") for s in report.shortfalls)


def test_week_matching_window() -> None:
    incident = {"start_date": "2026-05-04", "end_date": None}
    assert _days_from_incident(incident, date(2026, 5, 18)) == 0
    assert _days_from_incident(incident, date(2026, 4, 27)) == 7
    bounded = {"start_date": "2026-06-08", "end_date": "2026-06-09"}
    assert _days_from_incident(bounded, date(2026, 6, 8) + timedelta(days=9)) == 8


def test_launches_are_never_decoys(detection: Detection, rc_questions: list[Question]) -> None:
    assert detection.decoys
    assert all(c.change_type != "launched" for c in detection.decoys)
    for q in rc_questions:
        if q.template in DECOY_ANGLES:
            assert "launch" not in q.question, q.id
