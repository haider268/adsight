from pathlib import Path
from typing import Any

import pytest

from adsight.benchmark.answer import AgentAnswer, NamedValue
from adsight.benchmark.questions import Question, RootCauseScoring, load_questions
from adsight.benchmark.score import matches, reference_answer, score


@pytest.fixture(scope="module")
def questions(questions_path: Path) -> dict[str, Question]:
    return {q.id: q for q in load_questions(questions_path)}


def _answer(**fields: Any) -> AgentAnswer:
    return AgentAnswer.model_validate({"kind": "answer", "summary": "x", **fields})


# --- matches ---------------------------------------------------------------


def test_usd_tolerance_is_one_percent() -> None:
    assert matches(7037.26, 7037.26, "usd")
    assert matches(7037.26, 7100.0, "usd")  # 0.9% off
    assert not matches(100.0, 102.0, "usd")  # 2% off
    assert matches(0.5, 0.509, "usd")  # floor 0.01 beats 1% of 0.5
    assert not matches(0.5, 0.52, "usd")


def test_count_floor() -> None:
    assert matches(3, 3.5, "count")
    assert not matches(3, 3.6, "count")


def test_ratio_given_as_percent() -> None:
    assert matches(0.0211, 2.11, "ratio")
    assert matches(0.0211, 0.0211, "ratio")
    assert not matches(0.0211, 2.11, "number")
    assert not matches(0.0211, 0.03, "ratio")


# --- the reference answer passes every question ----------------------------


def test_reference_answers_score_100_percent(
    questions_path: Path, bench_manifest: dict[str, Any]
) -> None:
    all_questions = load_questions(questions_path)
    results = {q.id: score(q, reference_answer(q, bench_manifest)) for q in all_questions}
    assert len(results) == len(all_questions)
    failed = {qid: r.reason for qid, r in results.items() if not r.passed}
    assert failed == {}
    assert all(r.reason == "" and r.method == "rule" for r in results.values())


# --- negative scoring ------------------------------------------------------


def test_wrong_number_fails(questions: dict[str, Question]) -> None:
    result = score(questions["LK01"], _answer(values=[NamedValue(label="spend", value=7500.0)]))
    assert not result.passed
    assert "total_spend" in result.reason


def test_right_number_wrong_kind_fails(questions: dict[str, Question]) -> None:
    answer = AgentAnswer(
        kind="cannot_answer", summary="x", values=[NamedValue(label="spend", value=7037.26)]
    )
    result = score(questions["LK01"], answer)
    assert not result.passed
    assert "cannot_answer" in result.reason


def test_numeric_set_needs_every_value(questions: dict[str, Question]) -> None:
    result = score(questions["CP01"], _answer(values=[NamedValue(label="A1 image", value=0.0178)]))
    assert not result.passed
    assert "A3 video" in result.reason


def test_entity_by_id_or_name(questions: dict[str, Question]) -> None:
    assert score(questions["AG01"], _answer(entities=["c5"])).passed
    assert score(questions["AG01"], _answer(entities=[" Laser Hair Removal "])).passed
    wrong = score(questions["AG01"], _answer(entities=["C4", "C5"]))
    assert not wrong.passed
    assert "C4" in wrong.reason
    assert not score(questions["AG01"], _answer(entities=[])).passed


def test_wrong_trend_direction_fails(questions: dict[str, Question]) -> None:
    values = [NamedValue(label="first", value=15.80), NamedValue(label="last", value=14.51)]
    assert score(questions["TR01"], _answer(direction="down", values=values)).passed
    result = score(questions["TR01"], _answer(direction="up", values=values))
    assert not result.passed
    assert "direction" in result.reason


def test_trend_needs_both_endpoints(questions: dict[str, Question]) -> None:
    values = [NamedValue(label="first", value=15.80)]
    assert not score(questions["TR01"], _answer(direction="down", values=values)).passed


@pytest.mark.parametrize("incident_id", ["I2", "I3", "I4", "I5"])
def test_rc01_rejects_other_incident_causes(
    questions: dict[str, Question], bench_manifest: dict[str, Any], incident_id: str
) -> None:
    cause = next(i["cause"] for i in bench_manifest["incidents"] if i["incident_id"] == incident_id)
    assert not score(questions["RC01"], _answer(summary=cause)).passed


def test_rc01_accepts_the_ad_name_instead_of_its_id(questions: dict[str, Question]) -> None:
    summary = "The Women 30-55 Broad | Video ad was paused, so its budget went to weaker ads."
    assert score(questions["RC01"], _answer(summary=summary)).passed


def test_am01_needs_clarify_or_assumptions(questions: dict[str, Question]) -> None:
    assert not score(questions["AM01"], _answer()).passed
    assert score(questions["AM01"], _answer(assumptions=["All accounts, last 30 days"])).passed


def test_un01_refusal_must_not_carry_values(questions: dict[str, Question]) -> None:
    fabricated = AgentAnswer(
        kind="cannot_answer", summary="x", values=[NamedValue(label="roas", value=3.2)]
    )
    result = score(questions["UN01"], fabricated)
    assert not result.passed
    assert not score(questions["UN01"], _answer(values=[])).passed


def test_sql_question_without_expected_raises(questions: dict[str, Question]) -> None:
    bare = questions["LK01"].model_copy(update={"expected": None})
    with pytest.raises(ValueError, match="no expected answer"):
        score(bare, _answer())


# --- one-to-one matching and the ratio floor --------------------------------


def _numeric_question(qid: str, unit: str, values: dict[str, float]) -> Question:
    category, scoring_type = (
        ("comparison", "numeric_set")
        if qid.startswith("CP")
        else (
            "lookup",
            "numeric",
        )
    )
    return Question.model_validate(
        {
            "id": qid,
            "category": category,
            "question": "q",
            "scoring": {"type": scoring_type, "unit": unit},
            "reference_sql": f"{qid}.sql",
            "expected": {"values": values},
        }
    )


def _values(*numbers: float) -> list[NamedValue]:
    return [NamedValue(label=f"v{i}", value=n) for i, n in enumerate(numbers)]


def test_one_answer_value_satisfies_only_one_expected() -> None:
    q = _numeric_question("CP98", "usd", {"a": 100.0, "b": 100.5})
    result = score(q, _answer(values=_values(100.2)))
    assert not result.passed
    assert "unmatched expected values" in result.reason
    assert score(q, _answer(values=_values(100.2, 100.4))).passed


def test_matching_finds_assignment_greedy_would_miss() -> None:
    q = _numeric_question("CP97", "number", {"b": 101.5, "a": 100.0})
    assert score(q, _answer(values=_values(100.8, 102.4))).passed


def test_trend_endpoints_need_two_values() -> None:
    q = Question.model_validate(
        {
            "id": "TR98",
            "category": "trend",
            "question": "q",
            "scoring": {"type": "trend", "unit": "usd"},
            "reference_sql": "TR98.sql",
            "expected": {"values": {"first": 10.0, "last": 10.0}, "direction": "flat"},
        }
    )
    one = score(q, _answer(direction="flat", values=_values(10.0)))
    assert not one.passed
    assert "unmatched expected values" in one.reason
    assert score(q, _answer(direction="flat", values=_values(10.0, 10.0))).passed


def test_ratio_floor_is_tighter() -> None:
    assert not matches(0.0184, 0.0181, "ratio")
    assert matches(0.0184, 0.01845, "ratio")


# --- root cause: multi-incident and no-incident ground truth ----------------


def _root_cause_questions(questions_path: Path) -> list[Question]:
    return [q for q in load_questions(questions_path) if q.category == "root_cause"]


def _causes(manifest: dict[str, Any]) -> dict[str, str]:
    return {i["incident_id"]: i["cause"] for i in manifest["incidents"]}


def test_other_incident_causes_fail_every_root_cause_question(
    questions_path: Path, bench_manifest: dict[str, Any]
) -> None:
    causes = _causes(bench_manifest)
    checked = 0
    for q in _root_cause_questions(questions_path):
        assert isinstance(q.scoring, RootCauseScoring)
        if not q.scoring.incident_ids:
            continue
        for incident_id, cause in causes.items():
            if incident_id in q.scoring.incident_ids:
                continue
            result = score(q, _answer(summary=cause))
            assert not result.passed, (q.id, incident_id)
            checked += 1
    assert checked > 0


def test_incident_causes_fail_reference_summary_questions(
    questions_path: Path, bench_manifest: dict[str, Any]
) -> None:
    causes = _causes(bench_manifest)
    for q in _root_cause_questions(questions_path):
        assert isinstance(q.scoring, RootCauseScoring)
        if q.scoring.reference_summary is None:
            continue
        for incident_id, cause in causes.items():
            assert not score(q, _answer(summary=cause)).passed, (q.id, incident_id)


def _root_cause(scoring: dict[str, Any]) -> Question:
    return Question.model_validate(
        {
            "id": "RC99",
            "category": "root_cause",
            "question": "Why?",
            "scoring": {"type": "root_cause", **scoring},
        }
    )


def test_reference_answer_joins_causes_in_list_order(bench_manifest: dict[str, Any]) -> None:
    causes = _causes(bench_manifest)
    q = _root_cause({"incident_ids": ["I4", "I1"], "required": ["x"]})
    assert reference_answer(q, bench_manifest).summary == f"{causes['I4']} {causes['I1']}"


def test_reference_answer_prefers_reference_summary(bench_manifest: dict[str, Any]) -> None:
    q = _root_cause(
        {"incident_ids": [], "reference_summary": "Nothing changed.", "required": ["x"]}
    )
    answer = reference_answer(q, bench_manifest)
    assert answer.kind == "answer"
    assert answer.summary == "Nothing changed."


def test_reference_answer_unknown_incident_raises(bench_manifest: dict[str, Any]) -> None:
    q = _root_cause({"incident_ids": ["I9"], "required": ["x"]})
    with pytest.raises(ValueError, match="I9"):
        reference_answer(q, bench_manifest)


def test_rc20_needs_both_incidents(
    questions: dict[str, Question], bench_manifest: dict[str, Any]
) -> None:
    causes = _causes(bench_manifest)
    rc20 = questions["RC20"]
    assert not score(rc20, _answer(summary=causes["I1"])).passed
    assert not score(rc20, _answer(summary=causes["I4"])).passed
    joined = reference_answer(rc20, bench_manifest)
    assert joined.summary == f"{causes['I1']} {causes['I4']}"
    assert score(rc20, joined).passed
