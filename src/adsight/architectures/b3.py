"""B3 self-correction: B2 plus a fix loop. Max 5 LLM calls (plan, 3 fixes, answer).

plan -> (clarify / cannot_answer) -> finish_direct
     -> (query) -> execute -> check -> (issues, fewer than 3 fixes) -> fix -> execute ...
                                    -> answer
"""

from typing import Any

import duckdb
from langgraph.graph import END, START, StateGraph

from adsight.agent.prompts import system_prompt
from adsight.architectures.b2 import answer_node
from adsight.architectures.common import (
    AgentState,
    Meter,
    Runtime,
    check,
    execute_node,
    finish_direct,
    fix_node,
    needs_fix,
    plan_node,
    route_plan,
)
from adsight.llm.base import LLMProvider
from adsight.llm.types import Effort
from adsight.semantic.model import SemanticModel


def route_check(state: AgentState) -> str:
    return "fix" if needs_fix(state) else "answer"


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
    graph.add_node("fix", fix_node(rt))
    graph.add_node("answer", answer_node(rt))
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan", route_plan, {"execute": "execute", "finish_direct": "finish_direct"}
    )
    graph.add_edge("execute", "check")
    graph.add_conditional_edges("check", route_check, {"fix": "fix", "answer": "answer"})
    graph.add_edge("fix", "execute")
    graph.add_edge("answer", END)
    graph.add_edge("finish_direct", END)
    return graph.compile()
