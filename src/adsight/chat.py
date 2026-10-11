"""Multi-turn chat on top of the one-question architectures.

The agents answer one question at a time and remember nothing. ChatSession turns a
conversation into one question: the new user message plus a short "Conversation so far"
block, all in the user part of the prompt, so the cached system prefix (role + schema
card) stays byte-identical to a benchmark run. Same LLM layer, cache, budget guard and SQL
safety as the benchmark; no batching, because a person is waiting.
"""

from pathlib import Path
from typing import Any

import duckdb

from adsight.agent.context import model_tables, schema_card
from adsight.architectures import ARCHITECTURES, run_question
from adsight.benchmark.answer import AgentAnswer, NamedValue
from adsight.config import Settings, get_settings
from adsight.generate.context import build_context
from adsight.llm.base import LLMProvider
from adsight.semantic.model import SemanticModel, load_model
from adsight.sql_safety import Render, open_agent_connection

HISTORY_MESSAGES = 6
HISTORY_CHARS = 400
RESOLVE_RULE = (
    "Resolve references such as 'it', 'that campaign' or 'last month' from the conversation; "
    "if a reference is still unclear, ask."
)
MONEY_WORDS = ("spend", "cost", "cpl", "cpc", "cpm", "budget", "revenue", "price", "usd", "$")
RATE_WORDS = ("ctr", "cvr", "rate", "click-through", "share", "percent", "pct")

History = list[tuple[str, str]]


def _trim(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= HISTORY_CHARS else text[: HISTORY_CHARS - 3] + "..."


def build_question(history: History) -> str:
    """The question the architecture sees: the last user message, then at most the last
    six earlier messages (each at most 400 characters) and how to resolve references."""
    if not history or history[-1][0] != "user":
        raise ValueError("history must end with a user message")
    question = history[-1][1].strip()
    earlier = history[:-1][-HISTORY_MESSAGES:]
    if not earlier:
        return question
    lines = [f"{'User' if role == 'user' else 'Agent'}: {_trim(text)}" for role, text in earlier]
    return "\n".join([question, "", "Conversation so far:", *lines, RESOLVE_RULE])


def _value_text(value: NamedValue) -> str:
    label = value.label.casefold()
    number = float(value.value)
    if any(w in label for w in MONEY_WORDS):
        return f"{number:,.2f}"
    if any(w in label for w in RATE_WORDS) and abs(number) <= 1:
        return f"{number * 100:.2f}%"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def render_reply(answer: AgentAnswer) -> str:
    """An answer for a person: the summary, each value as "label: value" (rounded for
    display only), then the assumptions. Never the SQL."""
    lines = [answer.summary.strip()]
    lines += [f"{v.label}: {_value_text(v)}" for v in answer.values]
    if answer.assumptions:
        lines.append(f"(Assumed: {'; '.join(answer.assumptions)})")
    return "\n".join(line for line in lines if line)


class ChatSession:
    def __init__(
        self,
        model_spec: str,
        arch: str = "b4",
        semantic: Path = Path("semantic/demo.yaml"),
        db: Path | None = None,
        render: Render = "v4",
        *,
        provider: LLMProvider | None = None,
        settings: Settings | None = None,
    ):
        if arch not in ARCHITECTURES:
            raise ValueError(f"unknown architecture {arch!r}")
        self.model_spec, self.arch, self.render = model_spec, arch, render
        self.settings = settings or get_settings()
        self.model: SemanticModel = load_model(semantic)
        self.db = db or self.settings.db_path
        if provider is None:
            from adsight.llm.factory import make_provider

            provider = make_provider(model_spec, self.settings, batch=False)
        self.provider = provider
        self._con: duckdb.DuckDBPyConnection | None = None
        self._card = ""

    def _connection(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            self._con = open_agent_connection(self.db)
            self._card = schema_card(self.model, build_context(self.model, self._con), self._con)
        return self._con

    async def answer(self, history: History) -> AgentAnswer:
        con = self._connection()
        result = await run_question(
            self.arch,
            build_question(history),
            self.provider,
            self.model_spec,
            self._card,
            con,
            set(model_tables(self.model)),
            semantic=self.model,
            render=self.render,
        )
        return result.answer

    async def reply(self, history: History) -> str:
        return render_reply(await self.answer(history))

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> "ChatSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
