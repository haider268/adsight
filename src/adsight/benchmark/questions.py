"""Benchmark questions: categories, scoring specs, and the JSONL file format.

The benchmark is defined on the seed-7 dataset (BENCHMARK_SEED). Expected values are
computed from reference SQL against that dataset and are only valid for it.
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

BENCHMARK_SEED = 7
DATA_START = date(2026, 1, 1)
DATA_END = date(2026, 6, 29)
BENCHMARK_TODAY = date(2026, 6, 30)

Category = Literal[
    "lookup", "aggregation", "comparison", "trend", "root_cause", "ambiguous", "unanswerable"
]
Unit = Literal["usd", "count", "ratio", "number"]

PREFIX_BY_CATEGORY: dict[str, str] = {
    "lookup": "LK",
    "aggregation": "AG",
    "comparison": "CP",
    "trend": "TR",
    "root_cause": "RC",
    "ambiguous": "AM",
    "unanswerable": "UN",
}
SCORING_BY_CATEGORY: dict[str, str] = {
    "lookup": "numeric",
    "aggregation": "entity",
    "comparison": "numeric_set",
    "trend": "trend",
    "root_cause": "root_cause",
    "ambiguous": "clarify",
    "unanswerable": "refuse",
}
SQL_SCORING_TYPES = frozenset({"numeric", "entity", "numeric_set", "trend"})

_ID_PATTERN = re.compile(r"^(LK|AG|CP|TR|RC|AM|UN)\d{2}$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class NumericScoring(_Frozen):
    type: Literal["numeric"] = "numeric"
    unit: Unit


class EntityScoring(_Frozen):
    type: Literal["entity"] = "entity"


class NumericSetScoring(_Frozen):
    type: Literal["numeric_set"] = "numeric_set"
    unit: Unit


class TrendScoring(_Frozen):
    type: Literal["trend"] = "trend"
    unit: Unit
    flat_band_pct: float = 5.0


class RootCauseScoring(_Frozen):
    type: Literal["root_cause"] = "root_cause"
    incident_ids: list[str]  # injected incidents behind the answer; empty for no-incident cases
    reference_summary: str | None = None  # the perfect answer when no incident explains it
    required: list[str]  # case-insensitive regexes that must match the summary
    forbidden: list[str] = []  # case-insensitive regexes that must not match it

    @model_validator(mode="after")
    def _has_ground_truth(self) -> "RootCauseScoring":
        if not self.incident_ids and self.reference_summary is None:
            raise ValueError("root_cause scoring needs incident_ids or a reference_summary")
        return self

    @field_validator("required", "forbidden")
    @classmethod
    def _regexes_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return patterns


class ClarifyScoring(_Frozen):
    type: Literal["clarify"] = "clarify"


class RefuseScoring(_Frozen):
    type: Literal["refuse"] = "refuse"
    reason: str


Scoring = Annotated[
    NumericScoring
    | EntityScoring
    | NumericSetScoring
    | TrendScoring
    | RootCauseScoring
    | ClarifyScoring
    | RefuseScoring,
    Field(discriminator="type"),
]


class Expected(_Frozen):
    values: dict[str, float] = {}
    entity_id: str | None = None
    entity_name: str | None = None
    direction: Literal["up", "down", "flat"] | None = None


class Question(_Frozen):
    id: str
    category: Category
    question: str
    scoring: Scoring
    reference_sql: str | None = None
    expected: Expected | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> "Question":
        if not _ID_PATTERN.match(self.id):
            raise ValueError(f"bad question id {self.id!r}")
        if self.id[:2] != PREFIX_BY_CATEGORY[self.category]:
            raise ValueError(f"id {self.id!r} does not match category {self.category!r}")
        allowed = SCORING_BY_CATEGORY[self.category]
        if self.scoring.type != allowed:
            raise ValueError(
                f"{self.category} questions use {allowed!r} scoring, not {self.scoring.type!r}"
            )
        if self.scoring.type in SQL_SCORING_TYPES:
            if not self.reference_sql:
                raise ValueError(f"{self.id}: {self.scoring.type} scoring needs reference_sql")
        elif self.reference_sql is not None:
            raise ValueError(f"{self.id}: {self.scoring.type} scoring takes no reference_sql")
        return self


def load_questions(path: Path) -> list[Question]:
    """Read one Question per non-empty JSONL line; ids must be unique."""
    questions: list[Question] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            question = Question.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
        if question.id in seen:
            raise ValueError(f"{path}:{lineno}: duplicate question id {question.id!r}")
        seen.add(question.id)
        questions.append(question)
    return questions


def save_questions(questions: list[Question], path: Path) -> None:
    """Write one compact JSON object per line, in the given order."""
    lines = [
        json.dumps(q.model_dump(mode="json", exclude_none=True), separators=(",", ":"))
        for q in questions
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
