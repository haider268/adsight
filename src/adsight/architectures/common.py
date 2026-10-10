"""What every architecture shares: the graph state, the LLM call, result formatting and the
plan -> execute -> check -> fix nodes used by B2, B3 and B4."""

from dataclasses import dataclass, field
from typing import Any, TypedDict, TypeVar

import duckdb
from pydantic import BaseModel, ValidationError

from adsight.agent.checks import check_result
from adsight.agent.prompts import FIX, FIX_MAX_TOKENS, PLAN, PLAN_MAX_TOKENS
from adsight.agent.schemas import FixJSON, PlanJSON
from adsight.benchmark.answer import AgentAnswer
from adsight.llm.base import InvalidLLMOutput, LLMProvider
from adsight.llm.factory import parse_spec
from adsight.llm.schema import portable_schema
from adsight.llm.types import Effort, LLMRequest, Msg
from adsight.semantic.model import SemanticModel
from adsight.sql_safety import SqlResult, run_sql

M = TypeVar("M", bound=BaseModel)

RETRY_SALT = "retry1"
MAX_PLAN_STEPS = 4
MAX_FIXES = 3
RESULT_ROWS = 25


class AgentState(TypedDict, total=False):
    question: str
    steps: list[dict[str, Any]]  # what happened, in order (plans, queries, results)
    llm_calls: int
    tokens: dict[str, int]  # {"input": n, "output": n}
    sql_list: list[str]  # every query the agent ran
    answer: AgentAnswer
    # B2-B4
    plan: dict[str, Any]  # {"kind", "reason"}
    plan_steps: list[dict[str, Any]]  # goal, sql, result, issues, pending
    fixes: int
    classification: dict[str, Any]  # {"kind", "reason"}
    diagnosis: str
    draft: dict[str, Any]  # the AnswerJSON before number verification
    untraceable: list[float]
    resynthesized: bool
    untraceable_numbers: int


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
    semantic: SemanticModel | None = None
    salt: str = ""  # run-level salt: repeat runs are independent samples


def retry_salt(salt: str) -> str:
    """The salt of the one retry after invalid output, combined with the run salt."""
    return f"{salt}:{RETRY_SALT}" if salt else RETRY_SALT


async def call_llm(rt: Runtime, schema: type[M], user_text: str, max_tokens: int) -> M:
    """One structured call validated into `schema`, cached under the run salt. Invalid
    output is retried once with "<salt>:retry1" (a new cache key); a second failure raises
    InvalidTwice."""
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
    for salt in (rt.salt, retry_salt(rt.salt)):
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


# --- plan -> execute -> check -> fix (B2, B3, B4) --------------------------------------


def render_steps(plan_steps: list[dict[str, Any]], with_issues: bool = True) -> str:
    """Each step's goal, SQL, first rows or error, and its check issues."""
    blocks = []
    for i, step in enumerate(plan_steps, 1):
        block = f"Step {i} goal: {step['goal']}\nSQL: {step['sql']}\n{step['text']}"
        if with_issues and step.get("issues"):
            block += "\nCheck issues: " + " ".join(step["issues"])
        blocks.append(block)
    return "\n\n".join(blocks)


def evidence_text(state: AgentState) -> str:
    """What the data said: each step's result (not its SQL or goal) and the diagnosis."""
    results = [str(step.get("text", "")) for step in state.get("plan_steps", [])]
    return "\n".join([*results, state.get("diagnosis", "")])


def plan_node(rt: Runtime) -> Any:  # Any: LangGraph's add_node overloads
    async def plan(state: AgentState) -> AgentState:
        p = await call_llm(rt, PlanJSON, PLAN.format(question=state["question"]), PLAN_MAX_TOKENS)
        plan_steps = [
            {"goal": s.goal, "sql": s.sql, "pending": True, "issues": []}
            for s in p.steps[:MAX_PLAN_STEPS]
        ]
        record = {"node": "plan", "kind": p.kind, "reason": p.reason, "steps": len(plan_steps)}
        return {
            "plan": {"kind": p.kind, "reason": p.reason},
            "plan_steps": plan_steps,
            "fixes": 0,
            "steps": [*state.get("steps", []), record],
            **rt.meter.fields(),
        }

    return plan


def route_plan(state: AgentState) -> str:
    return "execute" if state["plan"]["kind"] == "query" else "finish_direct"


def finish_direct(state: AgentState) -> AgentState:
    """A plan that asks to clarify or declines becomes the answer, with no SQL."""
    p = state["plan"]
    return {"answer": AgentAnswer(kind=p["kind"], summary=p["reason"])}


def execute_node(rt: Runtime) -> Any:
    def execute(state: AgentState) -> AgentState:
        plan_steps = [dict(s) for s in state["plan_steps"]]
        sql_list = list(state.get("sql_list", []))
        for step in plan_steps:
            if not step["pending"]:
                continue
            result = run_sql(rt.con, step["sql"], rt.allowed_tables)
            step.update(result=result, text=format_result(result, RESULT_ROWS), pending=False)
            step["rows"] = result.rows[:RESULT_ROWS]
            step["checked"] = False
            sql_list.append(step["sql"])
        return {"plan_steps": plan_steps, "sql_list": sql_list}

    return execute


def check(state: AgentState) -> AgentState:
    """Deterministic checks on every step executed since the last check."""
    plan_steps = [dict(s) for s in state["plan_steps"]]
    for step in plan_steps:
        if not step.get("checked", True):
            step["issues"] = check_result(step["goal"], step["result"])
            step["checked"] = True
    return {"plan_steps": plan_steps}


def needs_fix(state: AgentState) -> bool:
    return state.get("fixes", 0) < MAX_FIXES and any(s["issues"] for s in state["plan_steps"])


def fix_node(rt: Runtime) -> Any:
    async def fix(state: AgentState) -> AgentState:
        plan_steps = [dict(s) for s in state["plan_steps"]]
        step = next(s for s in plan_steps if s["issues"])
        problem = "\n".join(step["issues"])
        text = FIX.format(goal=step["goal"], sql=step["sql"], problem=problem)
        f = await call_llm(rt, FixJSON, text, FIX_MAX_TOKENS)
        record = {"node": "fix", "goal": step["goal"], "diagnosis": f.diagnosis, "sql": f.sql}
        step.update(sql=f.sql, pending=True, issues=[])
        return {
            "plan_steps": plan_steps,
            "fixes": state.get("fixes", 0) + 1,
            "steps": [*state.get("steps", []), record],
            **rt.meter.fields(),
        }

    return fix
