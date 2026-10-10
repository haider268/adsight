"""B0: plan one query, run it once, answer from the result. At most 2 LLM calls.

plan -> (clarify / cannot_answer) -> finish_direct
     -> (query) -> execute -> answer
"""

from typing import Any

import duckdb
from langgraph.graph import END, START, StateGraph

from adsight.agent.prompts import (
    ANSWER_MAX_TOKENS,
    B0_ANSWER,
    B0_PLAN,
    PLAN_MAX_TOKENS,
    system_prompt,
)
from adsight.agent.schemas import AnswerJSON, B0Plan, to_agent_answer
from adsight.architectures.common import AgentState, Meter, Runtime, call_llm, format_result
from adsight.benchmark.answer import AgentAnswer
from adsight.llm.base import LLMProvider
from adsight.llm.types import Effort
from adsight.semantic.model import SemanticModel
from adsight.sql_safety import run_sql

ANSWER_ROWS = 25


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
    salt: str = "",
) -> Any:
    rt = Runtime(provider, model_spec, system_prompt(card), con, allowed_tables, effort)
    rt.salt = salt
    if meter is not None:
        rt.meter = meter

    async def plan(state: AgentState) -> AgentState:
        p = await call_llm(rt, B0Plan, B0_PLAN.format(question=state["question"]), PLAN_MAX_TOKENS)
        step = {"node": "plan", **p.model_dump()}
        return {"steps": [*state.get("steps", []), step], **rt.meter.fields()}

    def route(state: AgentState) -> str:
        return "execute" if state["steps"][-1]["kind"] == "query" else "finish_direct"

    def finish_direct(state: AgentState) -> AgentState:
        p = state["steps"][-1]
        return {"answer": AgentAnswer(kind=p["kind"], summary=p["reason"])}

    def execute(state: AgentState) -> AgentState:
        sql = state["steps"][-1]["sql"]
        result = run_sql(rt.con, sql, rt.allowed_tables)
        step = {"node": "execute", "sql": sql, "result": format_result(result, ANSWER_ROWS)}
        return {
            "steps": [*state["steps"], step],
            "sql_list": [*state.get("sql_list", []), sql],
        }

    async def answer(state: AgentState) -> AgentState:
        last = state["steps"][-1]
        text = B0_ANSWER.format(question=state["question"], sql=last["sql"], result=last["result"])
        a = await call_llm(rt, AnswerJSON, text, ANSWER_MAX_TOKENS)
        return {"answer": to_agent_answer(a, state["sql_list"]), **rt.meter.fields()}

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan)
    graph.add_node("finish_direct", finish_direct)
    graph.add_node("execute", execute)
    graph.add_node("answer", answer)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan", route, {"execute": "execute", "finish_direct": "finish_direct"}
    )
    graph.add_edge("execute", "answer")
    graph.add_edge("answer", END)
    graph.add_edge("finish_direct", END)
    return graph.compile()
