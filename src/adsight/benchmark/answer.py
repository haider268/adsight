"""The answer contract: what every agent architecture returns for a benchmark question."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class NamedValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    value: float


class AgentAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["answer", "clarify", "cannot_answer"]
    summary: str
    values: list[NamedValue] = []
    entities: list[str] = []  # ids or names, best first
    direction: Literal["up", "down", "flat"] | None = None
    assumptions: list[str] = []
    sql: list[str] = []  # queries executed, informational only
