"""The JSON shapes the LLM must return. Every one is sent through portable_schema, so
optional values are "" or [] and "none" stands for no direction (never null)."""

from typing import Literal

from pydantic import BaseModel

from adsight.benchmark.answer import AgentAnswer, NamedValue


class ValueJSON(BaseModel):
    label: str
    value: float


class AnswerJSON(BaseModel):
    kind: Literal["answer", "clarify", "cannot_answer"]
    summary: str
    values: list[ValueJSON]
    entities: list[str]
    direction: Literal["up", "down", "flat", "none"]
    assumptions: list[str]


class B0Plan(BaseModel):
    kind: Literal["query", "clarify", "cannot_answer"]
    sql: str
    reason: str


class ReActStep(BaseModel):
    """When action is run_sql, `final` is an empty answer (kind "answer", empty strings and
    lists, direction "none")."""

    thought: str
    action: Literal["run_sql", "final"]
    sql: str
    final: AnswerJSON


class PlanStep(BaseModel):
    goal: str
    sql: str


class PlanJSON(BaseModel):
    """1 to 4 steps when kind is query; otherwise steps is empty and reason explains."""

    kind: Literal["query", "clarify", "cannot_answer"]
    reason: str
    steps: list[PlanStep]


class FixJSON(BaseModel):
    diagnosis: str
    sql: str


class ClassifyJSON(BaseModel):
    kind: Literal["lookup", "analytical", "diagnostic", "ambiguous", "unanswerable"]
    reason: str


EMPTY_ANSWER = AnswerJSON(
    kind="answer", summary="", values=[], entities=[], direction="none", assumptions=[]
)


def to_agent_answer(a: AnswerJSON, sql_list: list[str]) -> AgentAnswer:
    return AgentAnswer(
        kind=a.kind,
        summary=a.summary,
        values=[NamedValue(label=v.label, value=v.value) for v in a.values],
        entities=list(a.entities),
        direction=None if a.direction == "none" else a.direction,
        assumptions=list(a.assumptions),
        sql=list(sql_list),
    )
