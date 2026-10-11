import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adsight.benchmark.answer import AgentAnswer, NamedValue
from adsight.chat import RESOLVE_RULE, ChatSession, build_question, render_reply
from adsight.cli import app
from adsight.config import Settings
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest
from adsight.probe_adapter import make_agent

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
COUNT_SQL = "SELECT COUNT(*) AS ads FROM ads"

runner = CliRunner()


def script(req: LLMRequest) -> dict[str, Any]:
    if req.schema_name == "ClassifyJSON":
        return {"kind": "lookup", "reason": "one number"}
    if req.schema_name == "PlanJSON":
        return {"kind": "query", "reason": "", "steps": [{"goal": "count", "sql": COUNT_SQL}]}
    return {
        "kind": "answer",
        "summary": "There are 54 ads.",
        "values": [{"label": "ads", "value": 54}],
        "entities": [],
        "direction": "none",
        "assumptions": [],
    }


@pytest.fixture
def session(bench_data_dir: Path) -> Iterator[tuple[ChatSession, ScriptedProvider]]:
    provider = ScriptedProvider(script)
    settings = Settings(_env_file=None, data_dir=bench_data_dir)  # type: ignore[call-arg]
    chat = ChatSession(
        "fixture:scripted",
        "b4",
        DEMO_YAML,
        bench_data_dir / "adsight.duckdb",
        provider=provider,
        settings=settings,
    )
    yield chat, provider
    chat.close()


def test_follow_up_carries_the_conversation_in_the_user_message(
    session: tuple[ChatSession, ScriptedProvider],
) -> None:
    chat, provider = session
    first = asyncio.run(chat.reply([("user", "How many ads are there?")]))
    assert first == "There are 54 ads.\nads: 54"
    single = list(provider.requests)
    history = [("user", "How many ads are there?"), ("agent", first), ("user", "And last month?")]
    asyncio.run(chat.reply(history))
    follow_up = provider.requests[len(single) :]
    text = follow_up[0].messages[-1].text
    assert "And last month?" in text and "Conversation so far:" in text
    assert "User: How many ads are there?" in text
    assert "Agent: There are 54 ads. ads: 54" in text and RESOLVE_RULE in text
    assert {r.system for r in follow_up} == {single[0].system}  # cached prefix unchanged
    assert "Conversation so far" not in single[0].messages[-1].text


def test_history_is_trimmed() -> None:
    long = "x" * 1000
    history = [("user" if i % 2 == 0 else "agent", f"m{i} {long}") for i in range(9)]
    history.append(("user", "now?"))
    question = build_question(history)
    lines = question.splitlines()
    assert lines[0] == "now?"
    kept = [line for line in lines if line.startswith(("User: ", "Agent: "))]
    assert len(kept) == 6 and kept[0].startswith("Agent: m3 ")
    assert all(len(line.split(": ", 1)[1]) == 400 for line in kept)
    assert build_question([("user", "only one")]) == "only one"
    with pytest.raises(ValueError):
        build_question([("agent", "no user last")])


def test_reply_rendering() -> None:
    answer = AgentAnswer(
        kind="answer",
        summary="Lumen Med Spa spent more in May.",
        values=[
            NamedValue(label="spend", value=1234.5678),
            NamedValue(label="cost per lead", value=12.3456),
            NamedValue(label="CTR", value=0.012345),
            NamedValue(label="leads", value=345.0),
            NamedValue(label="frequency", value=2.3456),
        ],
        assumptions=["May means May 2026", "all campaigns"],
        sql=["SELECT 1"],
    )
    assert render_reply(answer) == (
        "Lumen Med Spa spent more in May.\n"
        "spend: 1,234.57\n"
        "cost per lead: 12.35\n"
        "CTR: 1.23%\n"
        "leads: 345\n"
        "frequency: 2.35\n"
        "(Assumed: May means May 2026; all campaigns)"
    )
    assert "SELECT" not in render_reply(answer)
    refused = AgentAnswer(kind="cannot_answer", summary="There is no revenue data.")
    assert render_reply(refused) == "There is no revenue data."


@dataclass
class Message:
    role: str
    text: str


def test_probe_adapter_takes_any_role_and_text_objects() -> None:
    seen: list[list[tuple[str, str]]] = []

    class FakeSession:
        def __init__(self, spec: str):
            self.spec = spec

        async def reply(self, history: list[tuple[str, str]]) -> str:
            seen.append(history)
            return f"{self.spec}: ok"

    agent = make_agent("fixture:scripted", session_factory=FakeSession)  # type: ignore[arg-type]
    history = [Message("user", "hi"), Message("agent", "hello"), Message("user", "spend?")]
    assert asyncio.run(agent(history)) == "fixture:scripted: ok"
    assert seen == [[("user", "hi"), ("agent", "hello"), ("user", "spend?")]]


def test_probe_adapter_reads_the_model_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    specs: list[str] = []

    class FakeSession:
        def __init__(self, spec: str):
            specs.append(spec)

        async def reply(self, history: list[tuple[str, str]]) -> str:
            return "ok"

    agent = make_agent(session_factory=FakeSession)  # type: ignore[arg-type]
    monkeypatch.setenv("ADSIGHT_CHAT_MODEL", "groq:openai/gpt-oss-20b")
    asyncio.run(agent([Message("user", "hi")]))
    asyncio.run(agent([Message("user", "hi again")]))
    assert specs == ["groq:openai/gpt-oss-20b"]


def test_cli_chat_loop(
    bench_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adsight.llm.factory

    monkeypatch.setenv("ADSIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        adsight.llm.factory, "make_provider", lambda *a, **k: ScriptedProvider(script)
    )
    args = ["chat", "--model", "groq:openai/gpt-oss-120b"]
    args += ["--semantic", str(DEMO_YAML), "--db", str(bench_data_dir / "adsight.duckdb")]
    result = runner.invoke(app, args, input="How many ads?\nexit\n")
    assert result.exit_code == 0, result.output
    assert "adsight> There are 54 ads." in result.output
    assert "SELECT" not in result.output
