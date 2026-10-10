"""What every architecture shares: the graph state, the LLM call and result formatting."""

from dataclasses import dataclass, field
from typing import Any, TypedDict, TypeVar

import duckdb
from pydantic import BaseModel, ValidationError

from adsight.benchmark.answer import AgentAnswer
from adsight.llm.base import InvalidLLMOutput, LLMProvider
from adsight.llm.factory import parse_spec
from adsight.llm.schema import portable_schema
from adsight.llm.types import Effort, LLMRequest, Msg
from adsight.sql_safety import SqlResult

M = TypeVar("M", bound=BaseModel)

RETRY_SALT = "retry1"


class AgentState(TypedDict, total=False):
    question: str
    steps: list[dict[str, Any]]  # what happened, in order (plans, queries, results)
    llm_calls: int
    tokens: dict[str, int]  # {"input": n, "output": n}
    sql_list: list[str]  # every query the agent ran
    answer: AgentAnswer


@dataclass
class Meter:
    """Usage of one question, across every call including retries."""

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def fields(self) -> AgentState:
        return {
            "llm_calls": self.llm_calls,
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
        }


class InvalidTwice(Exception):
    """The model returned invalid output on the first try and on the salted retry."""


@dataclass
class Runtime:
    """Everything a graph's nodes need besides the state."""

    provider: LLMProvider
    model_spec: str
    system: str
    con: duckdb.DuckDBPyConnection
    allowed_tables: set[str]
    effort: Effort | None = None
    meter: Meter = field(default_factory=Meter)


async def call_llm(rt: Runtime, schema: type[M], user_text: str, max_tokens: int) -> M:
    """One structured call validated into `schema`. Invalid output is retried once with
    salt "retry1" (a new cache key); a second failure raises InvalidTwice."""
    spec = parse_spec(rt.model_spec)
    req = LLMRequest(
        provider=spec.provider,
        model=spec.model,
        system=rt.system,
        messages=[Msg(role="user", text=user_text)],
        schema=portable_schema(schema),
        schema_name=schema.__name__,
        max_tokens=max_tokens,
        effort=rt.effort,
    )
    for salt in ("", RETRY_SALT):
        rt.meter.llm_calls += 1
        try:
            resp = await rt.provider.complete(req, salt)
        except InvalidLLMOutput:
            continue
        rt.meter.input_tokens += resp.input_tokens + resp.cache_read_tokens
        rt.meter.input_tokens += resp.cache_write_tokens
        rt.meter.output_tokens += resp.output_tokens
        rt.meter.cost_usd += resp.cost_usd
        try:
            return schema.model_validate(resp.data)
        except ValidationError:
            continue
    raise InvalidTwice("invalid_output")


def format_result(result: SqlResult, max_rows: int) -> str:
    """A query result as the model sees it: columns, the first rows, the row count."""
    if result.error is not None:
        return f"Error: {result.error}"
    shown = result.rows[:max_rows]
    lines = ["Columns: " + " | ".join(result.columns)]
    lines += [" | ".join("NULL" if v is None else str(v) for v in row) for row in shown]
    more = " or more (truncated)" if result.truncated else ""
    lines.append(f"Rows: {result.row_count}{more}; showing {len(shown)}.")
    return "\n".join(lines)
