from pathlib import Path
from typing import Any

import pytest

from adsight.benchmark.answer import AgentAnswer, NamedValue
from adsight.benchmark.questions import Question, load_questions
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
    questions: dict[str, Question], bench_manifest: dict[str, Any]
) -> None:
    results = {qid: score(q, reference_answer(q, bench_manifest)) for qid, q in questions.items()}
    assert len(results) == 7
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
