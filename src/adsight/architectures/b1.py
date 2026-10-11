"""B1: a JSON-action ReAct loop. At most 6 think calls.

think -> (run_sql) -> act -> think ...
      -> (final, or the step limit) -> END
"""

from typing import Any

import duckdb
from langgraph.graph import END, START, StateGraph

from adsight.agent.prompts import (
    B1_LAST_STEP,
    B1_STEP,
    NO_STEPS,
    STEP_MAX_TOKENS,
    system_prompt,
)
from adsight.agent.schemas import ReActStep, to_agent_answer
from adsight.architectures.common import AgentState, Meter, Runtime, call_llm, format_result
from adsight.benchmark.answer import AgentAnswer
from adsight.llm.base import LLMProvider
from adsight.llm.types import Effort
from adsight.semantic.model import SemanticModel
from adsight.sql_safety import Render, run_sql

STEP_LIMIT_SUMMARY = "step limit reached"


def scratchpad(steps: list[dict[str, Any]]) -> str:
    acts = [s for s in steps if s["node"] == "act"]
    if not acts:
        return NO_STEPS
    return "\n\n".join(
        f"Step {i}: {a['thought']}\nSQL: {a['sql']}\n{a['result']}" for i, a in enumerate(acts, 1)
    )


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
    render: Render = "v4",
) -> Any:
    rt = Runtime(provider, model_spec, system_prompt(card), con, allowed_tables, effort)
    rt.salt = salt
    rt.render = render
    rt.semantic = semantic
    if meter is not None:
        rt.meter = meter

    async def think(state: AgentState) -> AgentState:
        steps = state.get("steps", [])
        n = sum(1 for s in steps if s["node"] == "think") + 1
        text = B1_STEP.format(question=state["question"], scratchpad=scratchpad(steps))
        if n == rt.tuning.b1_max_steps:
            text += B1_LAST_STEP
        step = await call_llm(rt, ReActStep, text, STEP_MAX_TOKENS)
        record = {"node": "think", "n": n, "thought": step.thought, "action": step.action}
        update: AgentState = {"steps": [*steps, {**record, "sql": step.sql}], **rt.meter.fields()}
        sql_list = state.get("sql_list", [])
        if step.action == "final":
            update["answer"] = to_agent_answer(step.final, sql_list)
        elif n >= rt.tuning.b1_max_steps:
            update["answer"] = AgentAnswer(
                kind="cannot_answer", summary=STEP_LIMIT_SUMMARY, sql=sql_list
            )
        return update

    def route(state: AgentState) -> str:
        return END if "answer" in state else "act"

    def act(state: AgentState) -> AgentState:
        last = state["steps"][-1]
        result = run_sql(rt.con, last["sql"], rt.allowed_tables, render=rt.render)
        step = {
            "node": "act",
            "thought": last["thought"],
            "sql": last["sql"],
            "result": format_result(result, rt.tuning.b1_scratchpad_rows),
        }
        return {
            "steps": [*state["steps"], step],
            "sql_list": [*state.get("sql_list", []), last["sql"]],
        }

    graph = StateGraph(AgentState)
    graph.add_node("think", think)
    graph.add_node("act", act)
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", route, {"act": "act", END: END})
    graph.add_edge("act", "think")
    return graph.compile()
