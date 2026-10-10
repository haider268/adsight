"""Agent architectures, each a compiled LangGraph graph, and `run_question` over them."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import duckdb

from adsight.architectures import b0, b1
from adsight.architectures.common import InvalidTwice, Meter
from adsight.benchmark.answer import AgentAnswer
from adsight.llm.base import LLMProvider
from adsight.llm.types import Effort

ARCHITECTURES: dict[str, Callable[..., Any]] = {"b0": b0.build_graph, "b1": b1.build_graph}


@dataclass(frozen=True)
class RunResult:
    answer: AgentAnswer
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    error: str | None = None


async def run_question(
    arch: str,
    question: str,
    provider: LLMProvider,
    model_spec: str,
    card: str,
    con: duckdb.DuckDBPyConnection,
    allowed_tables: set[str],
    effort: Effort | None = None,
) -> RunResult:
    """Run one question through one architecture. Provider errors other than invalid
    output (daily limit, budget, cache miss, transient) propagate to the caller."""
    meter = Meter()
    graph = ARCHITECTURES[arch](
        provider, model_spec, card, con, allowed_tables, effort=effort, meter=meter
    )
    started = time.perf_counter()
    error: str | None = None
    try:
        final = await graph.ainvoke({"question": question, "steps": [], "sql_list": []})
        answer: AgentAnswer = final["answer"]
    except InvalidTwice:
        error = "invalid_output"
        answer = AgentAnswer(kind="cannot_answer", summary="the model returned invalid output")
    return RunResult(
        answer=answer,
        llm_calls=meter.llm_calls,
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
        cost_usd=meter.cost_usd,
        latency_ms=(time.perf_counter() - started) * 1000,
        error=error,
    )
