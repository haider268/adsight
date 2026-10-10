"""B2 planner: plan 1-4 queries, run them all, check the results, answer. Max 2 LLM calls.

plan -> (clarify / cannot_answer) -> finish_direct
     -> (query) -> execute -> check -> answer
Check issues are shown to the answer step but never acted on.
"""

from typing import Any

import duckdb
from langgraph.graph import END, START, StateGraph

from adsight.agent.prompts import ANSWER_MAX_TOKENS, PLAN_ANSWER, system_prompt
from adsight.agent.schemas import AnswerJSON, to_agent_answer
from adsight.architectures.common import (
    AgentState,
    Meter,
    Runtime,
    call_llm,
    check,
    execute_node,
    finish_direct,
    plan_node,
    render_steps,
    route_plan,
)
from adsight.llm.base import LLMProvider
from adsight.llm.types import Effort
from adsight.semantic.model import SemanticModel


def answer_node(rt: Runtime) -> Any:
    async def answer(state: AgentState) -> AgentState:
        text = PLAN_ANSWER.format(
            question=state["question"], results=render_steps(state["plan_steps"])
        )
        a = await call_llm(rt, AnswerJSON, text, ANSWER_MAX_TOKENS)
        return {"answer": to_agent_answer(a, state.get("sql_list", [])), **rt.meter.fields()}

    return answer


def build_graph(
    provider: LLMProvider,
    model_spec: str,
    card: str,
    con: duckdb.DuckDBPyConnection,
    allowed_tables: set[str],
    *,
    effort: Effort | None = None,
    meter: Meter | None = None,
    semantic: SemanticModel | None = None,
) -> Any:
    rt = Runtime(provider, model_spec, system_prompt(card), con, allowed_tables, effort)
    if meter is not None:
        rt.meter = meter

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node(rt))
    graph.add_node("finish_direct", finish_direct)
    graph.add_node("execute", execute_node(rt))
    graph.add_node("check", check)
    graph.add_node("answer", answer_node(rt))
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan", route_plan, {"execute": "execute", "finish_direct": "finish_direct"}
    )
    graph.add_edge("execute", "check")
    graph.add_edge("check", "answer")
    graph.add_edge("answer", END)
    graph.add_edge("finish_direct", END)
    return graph.compile()
