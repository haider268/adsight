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

MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
MONTH_NUMBERS = {name: i for i, full in enumerate(MONTHS, 1) for name in (full, full[:3])}
QUOTES = "\"'`\u2018\u2019\u201c\u201d"  # straight and curly quotes


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


def normalise_entity(text: str) -> str:
    """One form for an entity name: no surrounding quotes, casefolded, underscores as
    spaces, single spaces; months as YYYY-MM ("May 2026", "2026-05-01" -> "2026-05")."""
    name = " ".join(text.strip().strip(QUOTES).replace("_", " ").casefold().split())
    iso = re.fullmatch(r"(\d{4})-(\d{2})(?:-01)?", name)
    if iso:
        return f"{iso[1]}-{iso[2]}"
    worded = re.fullmatch(r"([a-z]+)\.? (\d{4})", name)
    if worded and worded[1] in MONTH_NUMBERS:
        return f"{worded[2]}-{MONTH_NUMBERS[worded[1]]:02d}"
    return name


def _named_by_question(question: Question, entity: str) -> bool:
    """Whether the question itself names this (normalised) entity: in double quotes, as its
    scope, or as a phrase in its text (e.g. the account in "Which Lumen Med Spa ad set")."""
    quoted = {normalise_entity(n) for n in re.findall(r'"([^"]+)"', question.question)}
    scope = question.params.get("scope_entity")
    if entity in quoted or (scope is not None and entity == normalise_entity(scope)):
        return True
    text = " ".join(question.question.replace("_", " ").casefold().split())
    return re.search(rf"(?<!\w){re.escape(entity)}(?!\w)", text) is not None


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
        accepted = {
            normalise_entity(name) for name in (expected.entity_id, expected.entity_name) if name
        }
        given = [
            e
            for e in answer.entities
            if normalise_entity(e) in accepted
            or not _named_by_question(question, normalise_entity(e))
        ]
        if not given:
            return _fail("every entity given is one the question names")
        if normalise_entity(given[0]) not in accepted:
            return _fail(f"first entity {given[0]!r} is not {expected.entity_id}")
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
        if scoring.reference_summary is not None:
            return AgentAnswer(kind="answer", summary=scoring.reference_summary)
        causes = {str(i["incident_id"]): str(i["cause"]) for i in manifest["incidents"]}
        unknown = [iid for iid in scoring.incident_ids if iid not in causes]
        if unknown:
            raise ValueError(f"{question.id}: incidents not in the manifest: {unknown}")
        summary = " ".join(causes[iid] for iid in scoring.incident_ids)
        return AgentAnswer(kind="answer", summary=summary)
    if isinstance(scoring, ClarifyScoring):
        return AgentAnswer(kind="clarify", summary=CLARIFY_SUMMARY)
    if isinstance(scoring, RefuseScoring):
        return AgentAnswer(kind="cannot_answer", summary=scoring.reason)
    raise ValueError(f"unknown scoring type {scoring.type!r}")
