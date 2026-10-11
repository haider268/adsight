"""B4 full: classify, plan, execute, check, fix, diagnose (why questions), synthesize, and a
deterministic number check with one rewrite. Max 7 LLM calls: classify 1 + plan 1 + fix 3
+ synthesize 1 + resynthesize 1.

classify -> (ambiguous / unanswerable) -> finish_classified
         -> plan -> (clarify / cannot_answer) -> finish_direct
                 -> execute -> check -> (issues, fewer than 3 fixes) -> fix -> execute ...
                                     -> (diagnostic) -> diagnose -> synthesize
                                     -> synthesize -> verify_numbers -> (untraceable,
                                        first time) -> resynthesize -> verify_numbers -> END
"""

import json
from typing import Any, Literal

import duckdb
from langgraph.graph import END, START, StateGraph

from adsight.agent.diagnose import diagnose
from adsight.agent.numbers import untraceable_numbers
from adsight.agent.prompts import (
    ANSWER_MAX_TOKENS,
    CLASSIFY,
    CLASSIFY_MAX_TOKENS,
    RESYNTHESIZE,
    SYNTHESIZE,
    system_prompt,
)
from adsight.agent.schemas import AnswerJSON, ClassifyJSON, to_agent_answer
from adsight.architectures.common import (
    AgentState,
    Meter,
    Runtime,
    call_llm,
    check,
    evidence_text,
    execute_node,
    finish_direct,
    fix_node,
    needs_fix,
    plan_node,
    render_steps,
    route_plan,
)
from adsight.benchmark.answer import AgentAnswer
from adsight.llm.base import LLMProvider
from adsight.llm.types import Effort
from adsight.semantic.model import SemanticModel
from adsight.sql_safety import Render

NO_SEMANTIC = "Diagnosis: no semantic model is available, so no diagnosis was run."


def _synthesis_prompt(state: AgentState) -> str:
    diagnosis = state.get("diagnosis", "")
    return SYNTHESIZE.format(
        question=state["question"],
        results=render_steps(state.get("plan_steps", [])),
        diagnosis=f"{diagnosis}\n\n" if diagnosis else "",
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

    async def classify(state: AgentState) -> AgentState:
        text = CLASSIFY.format(question=state["question"])
        c = await call_llm(rt, ClassifyJSON, text, CLASSIFY_MAX_TOKENS)
        record = {"node": "classify", "kind": c.kind, "reason": c.reason}
        return {
            "classification": {"kind": c.kind, "reason": c.reason},
            "steps": [*state.get("steps", []), record],
            **rt.meter.fields(),
        }

    def route_classify(state: AgentState) -> str:
        kind = state["classification"]["kind"]
        return "finish_classified" if kind in ("ambiguous", "unanswerable") else "plan"

    def finish_classified(state: AgentState) -> AgentState:
        c = state["classification"]
        kind: Literal["clarify", "cannot_answer"] = (
            "clarify" if c["kind"] == "ambiguous" else "cannot_answer"
        )
        return {"answer": AgentAnswer(kind=kind, summary=c["reason"]), "untraceable_numbers": 0}

    def route_check(state: AgentState) -> str:
        if needs_fix(state, rt.tuning.max_fixes):
            return "fix"
        return "diagnose" if state["classification"]["kind"] == "diagnostic" else "synthesize"

    def diagnose_node(state: AgentState) -> AgentState:
        if rt.semantic is None:
            return {"diagnosis": NO_SEMANTIC}
        return {"diagnosis": diagnose(rt.semantic, rt.con, state["question"], state["plan_steps"])}

    async def synthesize(state: AgentState) -> AgentState:
        a = await call_llm(rt, AnswerJSON, _synthesis_prompt(state), ANSWER_MAX_TOKENS)
        return {"draft": a.model_dump(), **rt.meter.fields()}

    def verify_numbers(state: AgentState) -> AgentState:
        draft = AnswerJSON.model_validate(state["draft"])
        missing = untraceable_numbers(draft, state["question"], evidence_text(state))
        return {
            "untraceable": missing,
            "untraceable_numbers": len(missing),
            "answer": to_agent_answer(draft, state.get("sql_list", [])),
        }

    def route_verify(state: AgentState) -> str:
        if state["untraceable"] and not state.get("resynthesized", False):
            return "resynthesize"
        return END

    async def resynthesize(state: AgentState) -> AgentState:
        numbers = ", ".join(repr(float(n)) for n in state["untraceable"])
        previous = (
            _synthesis_prompt(state) + "\n\nYour previous answer:\n" + json.dumps(state["draft"])
        )
        text = RESYNTHESIZE.format(previous=previous, numbers=numbers)
        a = await call_llm(rt, AnswerJSON, text, ANSWER_MAX_TOKENS)
        return {"draft": a.model_dump(), "resynthesized": True, **rt.meter.fields()}

    graph = StateGraph(AgentState)
    graph.add_node("classify", classify)
    graph.add_node("finish_classified", finish_classified)
    graph.add_node("plan", plan_node(rt))
    graph.add_node("finish_direct", finish_direct)
    graph.add_node("execute", execute_node(rt))
    graph.add_node("check", check)
    graph.add_node("fix", fix_node(rt))
    graph.add_node("diagnose", diagnose_node)
    graph.add_node("synthesize", synthesize)
    graph.add_node("verify_numbers", verify_numbers)
    graph.add_node("resynthesize", resynthesize)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify", route_classify, {"plan": "plan", "finish_classified": "finish_classified"}
    )
    graph.add_conditional_edges(
        "plan", route_plan, {"execute": "execute", "finish_direct": "finish_direct"}
    )
    graph.add_edge("execute", "check")
    graph.add_conditional_edges(
        "check",
        route_check,
        {"fix": "fix", "diagnose": "diagnose", "synthesize": "synthesize"},
    )
    graph.add_edge("fix", "execute")
    graph.add_edge("diagnose", "synthesize")
    graph.add_edge("synthesize", "verify_numbers")
    graph.add_conditional_edges(
        "verify_numbers", route_verify, {"resynthesize": "resynthesize", END: END}
    )
    graph.add_edge("resynthesize", "verify_numbers")
    graph.add_edge("finish_classified", END)
    graph.add_edge("finish_direct", END)
    return graph.compile()
