"""Rule-based scoring of an AgentAnswer against a Question, and the perfect reference answer."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from adsight.benchmark.answer import AgentAnswer, NamedValue
from adsight.benchmark.questions import (
    ClarifyScoring,
    EntityScoring,
    Expected,
    NumericScoring,
    NumericSetScoring,
    Question,
    RefuseScoring,
    RootCauseScoring,
    TrendScoring,
    Unit,
)

FLOOR: dict[str, float] = {"usd": 0.01, "count": 0.5, "number": 0.01, "ratio": 0.0001}

CLARIFY_SUMMARY = "Which account, metric and date range do you mean?"


class ScoreResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    reason: str
    method: Literal["rule"]


def matches(expected: float, got: float, unit: Unit) -> bool:
    """Within 1% or the unit's floor; a ratio may also be given as a percent."""
    tol = max(abs(expected) * 0.01, FLOOR[unit])
    candidates = [got, got / 100] if unit == "ratio" else [got]
    return any(abs(c - expected) <= tol for c in candidates)


def _pass() -> ScoreResult:
    return ScoreResult(passed=True, reason="", method="rule")


def _fail(reason: str) -> ScoreResult:
    return ScoreResult(passed=False, reason=reason, method="rule")


def _unmatched(expected: dict[str, float], answer: AgentAnswer, unit: Unit) -> list[str]:
    """Expected labels left over after a maximum one-to-one matching to answer values.

    Each answer value may satisfy at most one expected value. Augmenting paths (Kuhn's
    algorithm) find the largest such matching, so a lucky order cannot hide a miss and
    an unlucky order cannot cause one.
    """
    labels = list(expected)
    got = [v.value for v in answer.values]
    candidates = [
        [j for j, value in enumerate(got) if matches(expected[label], value, unit)]
        for label in labels
    ]
    owner: list[int | None] = [None] * len(got)  # answer index -> expected index

    def assign(i: int, visited: set[int]) -> bool:
        for j in candidates[i]:
            if j in visited:
                continue
            visited.add(j)
            current = owner[j]
            if current is None or assign(current, visited):
                owner[j] = i
                return True
        return False

    for i in range(len(labels)):
        assign(i, set())
    matched = {i for i in owner if i is not None}
    return [label for i, label in enumerate(labels) if i not in matched]


def _missing_reason(missing: list[str]) -> str:
    return f"unmatched expected values: {', '.join(missing)} (each answer value counts once)"


def _expected(question: Question) -> Expected:
    if question.expected is None:
        raise ValueError(f"{question.id} has reference SQL but no expected answer")
    return question.expected


def score(question: Question, answer: AgentAnswer) -> ScoreResult:
    scoring = question.scoring

    if isinstance(scoring, NumericScoring | NumericSetScoring):
        expected = _expected(question)
        if answer.kind != "answer":
            return _fail(f"kind is {answer.kind!r}, expected 'answer'")
        missing = _unmatched(expected.values, answer, scoring.unit)
        if missing:
            return _fail(_missing_reason(missing))
        return _pass()

    if isinstance(scoring, EntityScoring):
        expected = _expected(question)
        if answer.kind != "answer":
            return _fail(f"kind is {answer.kind!r}, expected 'answer'")
        if not answer.entities:
            return _fail("no entities given")
        top = answer.entities[0].strip().casefold()
        accepted = {name.casefold() for name in (expected.entity_id, expected.entity_name) if name}
        if top not in accepted:
            return _fail(f"first entity {answer.entities[0]!r} is not {expected.entity_id}")
        return _pass()

    if isinstance(scoring, TrendScoring):
        expected = _expected(question)
        if answer.kind != "answer":
            return _fail(f"kind is {answer.kind!r}, expected 'answer'")
        if answer.direction != expected.direction:
            return _fail(f"direction is {answer.direction!r}, expected {expected.direction!r}")
        endpoints = {k: expected.values[k] for k in ("first", "last")}
        missing = _unmatched(endpoints, answer, scoring.unit)
        if missing:
            return _fail(_missing_reason(missing))
        return _pass()

    if isinstance(scoring, RootCauseScoring):
        if answer.kind != "answer":
            return _fail(f"kind is {answer.kind!r}, expected 'answer'")
        for pattern in scoring.required:
            if not re.search(pattern, answer.summary, re.IGNORECASE):
                return _fail(f"summary does not mention required /{pattern}/")
        for pattern in scoring.forbidden:
            if re.search(pattern, answer.summary, re.IGNORECASE):
                return _fail(f"summary matches forbidden /{pattern}/")
        return _pass()

    if isinstance(scoring, ClarifyScoring):
        if answer.kind == "clarify":
            return _pass()
        if answer.kind == "answer" and answer.assumptions:
            return _pass()
        return _fail("ambiguous question: neither asked to clarify nor stated assumptions")

    if isinstance(scoring, RefuseScoring):
        if answer.kind != "cannot_answer":
            return _fail(f"kind is {answer.kind!r}, expected 'cannot_answer'")
        if answer.values:
            return _fail("refused but still reported values")
        return _pass()

    raise ValueError(f"unknown scoring type {scoring.type!r}")


def reference_answer(question: Question, manifest: dict[str, Any]) -> AgentAnswer:
    """The perfect answer to a question, built from its expected values or the manifest."""
    scoring = question.scoring

    if isinstance(scoring, NumericScoring | NumericSetScoring):
        expected = _expected(question)
        return AgentAnswer(
            kind="answer",
            summary=f"Reference answer for {question.id}.",
            values=[NamedValue(label=k, value=v) for k, v in expected.values.items()],
        )
    if isinstance(scoring, EntityScoring):
        expected = _expected(question)
        if expected.entity_id is None:
            raise ValueError(f"{question.id} expected answer has no entity_id")
        return AgentAnswer(
            kind="answer",
            summary=f"Reference answer for {question.id}.",
            entities=[expected.entity_id],
        )
    if isinstance(scoring, TrendScoring):
        expected = _expected(question)
        return AgentAnswer(
            kind="answer",
            summary=f"Reference answer for {question.id}.",
            direction=expected.direction,
            values=[NamedValue(label=k, value=expected.values[k]) for k in ("first", "last")],
        )
    if isinstance(scoring, RootCauseScoring):
        incident = next(i for i in manifest["incidents"] if i["incident_id"] == scoring.incident_id)
        return AgentAnswer(kind="answer", summary=str(incident["cause"]))
    if isinstance(scoring, ClarifyScoring):
        return AgentAnswer(kind="clarify", summary=CLARIFY_SUMMARY)
    if isinstance(scoring, RefuseScoring):
        return AgentAnswer(kind="cannot_answer", summary=scoring.reason)
    raise ValueError(f"unknown scoring type {scoring.type!r}")
