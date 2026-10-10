import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from adsight.agent.context import model_tables, schema_card
from adsight.agent.prompts import B1_LAST_STEP
from adsight.agent.schemas import EMPTY_ANSWER, AnswerJSON, to_agent_answer
from adsight.architectures import RunResult, run_question
from adsight.benchmark.questions import Question, load_questions
from adsight.benchmark.score import score
from adsight.generate.context import build_context
from adsight.llm.base import InvalidLLMOutput
from adsight.llm.cache import CachedProvider, ResponseCache
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest
from adsight.semantic.model import SemanticModel, load_model
from adsight.sql_safety import open_agent_connection

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
FLAT_YAML = REPO_ROOT / "semantic" / "flat-demo.yaml"
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
SPEC = "fixture:scripted"


@pytest.fixture(scope="module")
def con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = open_agent_connection(bench_data_dir / "adsight.duckdb")
    yield c
    c.close()


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return load_model(DEMO_YAML)


@pytest.fixture(scope="module")
def card(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> str:
    return schema_card(model, build_context(model, con), con)


@pytest.fixture(scope="module")
def tables(model: SemanticModel) -> set[str]:
    return set(model_tables(model))


@pytest.fixture(scope="module")
def questions() -> dict[str, Question]:
    return {q.id: q for q in load_questions(DEMO_SET / "questions.jsonl")}


def ref_sql(q: Question) -> str:
    assert q.reference_sql is not None
    return (DEMO_SET / "reference_sql" / q.reference_sql).read_text(encoding="utf-8")


def first_row_values(text: str) -> list[dict[str, Any]]:
    """The model's job, scripted: read the first result row shown in a prompt."""
    lines = text.splitlines()
    start = max(i for i, line in enumerate(lines) if line.startswith("Columns: "))
    columns = lines[start][len("Columns: ") :].split(" | ")
    row = lines[start + 1].split(" | ")
    return [{"label": c, "value": float(v)} for c, v in zip(columns, row, strict=True)]


def answer(values: list[dict[str, Any]], kind: str = "answer") -> dict[str, Any]:
    return {
        "kind": kind,
        "summary": "From the query result.",
        "values": values,
        "entities": [],
        "direction": "none",
        "assumptions": [],
    }


def run(arch: str, q: str, provider: Any, card: str, con: Any, tables: set[str]) -> RunResult:
    return asyncio.run(run_question(arch, q, provider, SPEC, card, con, tables))


# --- schema card ----------------------------------------------------------------------


def test_card_is_stable_complete_and_short(
    model: SemanticModel, con: duckdb.DuckDBPyConnection, card: str
) -> None:
    assert schema_card(model, build_context(model, con), con) == card
    assert len(card) < 7500
    assert (
        'account (3): "Restore Regenerative Clinic", "Lumen Med Spa", "Northside Home Services"'
        in card
    )
    for line in ("campaign (9): ", "ad_set (18): ", 'ad (54): "Knee 45-65 Broad | Video", '):
        assert line in card, line
    assert card.count(", ...") >= 3
    assert (
        "Names in double quotes in a question are exact names from the data: filter on the "
        "label column with that exact string." in card
    )
    assert (
        "Ask a clarifying question only when the answer depends on a choice you would have to "
        "guess, such as which account, metric or period; otherwise answer." in card
    )
    mapped = [model.fact.date, *model.fact.measures.values()]
    for lv in model.levels:
        mapped += [c for c in (lv.key, lv.label, lv.parent_key) if c]
    mapped += [a.column for a in model.attributes]
    assert model.change_log is not None
    log = model.change_log
    mapped += [c for c in (log.date, log.entity_type, log.entity_id, log.change_type) if c]
    mapped += [c for c in (log.old_value, log.new_value) if c]
    for column in mapped:
        assert column in card, column
    assert "changes over time" in card
    assert "Every number you report must come from a query result." in card
    assert "2026-06-30" in card


def test_flat_card_has_no_reach_or_change_log(tmp_path: Path) -> None:
    from conftest import make_flat

    path = tmp_path / "flat.duckdb"
    make_flat(path)
    flat = load_model(FLAT_YAML)
    c = open_agent_connection(path)
    try:
        text = schema_card(flat, build_context(flat, c), c)
    finally:
        c.close()
    assert "reach" not in text and "frequency" not in text
    assert "change log" not in text and "events_log" not in text
    assert "perf" in text and "ad_group" in text
    assert "campaign (3): " in text and "ad_set (6): " in text


def test_to_agent_answer() -> None:
    a = to_agent_answer(EMPTY_ANSWER, ["SELECT 1"])
    assert a.direction is None and a.sql == ["SELECT 1"]
    up = AnswerJSON.model_validate({**answer([]), "direction": "up"})
    assert to_agent_answer(up, []).direction == "up"


# --- B0 ---------------------------------------------------------------------------------


def test_b0_answers_a_lookup(
    questions: dict[str, Question], card: str, con: Any, tables: set[str]
) -> None:
    q = questions["LK01"]

    def script(req: LLMRequest) -> dict[str, Any]:
        if req.schema_name == "B0Plan":
            return {"kind": "query", "sql": ref_sql(q), "reason": ""}
        return answer(first_row_values(req.messages[-1].text))

    provider = ScriptedProvider(script)
    result = run("b0", q.question, provider, card, con, tables)
    assert result.error is None and result.llm_calls == 2
    assert result.answer.sql == [ref_sql(q)]
    assert score(q, result.answer).passed, result.answer
    assert result.input_tokens > 0 and result.output_tokens > 0


def test_b0_clarify_makes_one_call(card: str, con: Any, tables: set[str]) -> None:
    plan = {"kind": "clarify", "sql": "", "reason": "Which account do you mean?"}
    provider = ScriptedProvider([plan])
    result = run("b0", "How did it do?", provider, card, con, tables)
    assert provider.calls == 1 and result.llm_calls == 1
    assert result.answer.kind == "clarify" and result.answer.sql == []
    assert result.answer.summary == "Which account do you mean?"


def test_b0_blocks_unsafe_sql(card: str, con: Any, tables: set[str]) -> None:
    provider = ScriptedProvider(
        [
            {"kind": "query", "sql": "DROP TABLE ads", "reason": ""},
            answer([], kind="cannot_answer"),
        ]
    )
    result = run("b0", "Drop everything", provider, card, con, tables)
    assert "Error: unsafe SQL: DROP is not allowed" in provider.requests[1].messages[-1].text
    assert result.answer.kind == "cannot_answer"
    row = con.execute("SELECT COUNT(*) FROM ads").fetchone()
    assert row is not None and row[0] == 54


def test_invalid_output_is_retried_once_then_reported(
    card: str, con: Any, tables: set[str], tmp_path: Path
) -> None:
    plan = {"kind": "cannot_answer", "sql": "", "reason": "No revenue column."}
    retried = ScriptedProvider([InvalidLLMOutput("oops"), plan])
    result = run("b0", "What was the ROAS?", retried, card, con, tables)
    assert result.error is None and result.llm_calls == 2
    assert result.answer.kind == "cannot_answer"

    cache = ResponseCache(tmp_path / "cache.sqlite")
    inner = ScriptedProvider([{"not": "a plan"}, plan])
    cached = CachedProvider(inner, cache)
    result = run("b0", "What was the ROAS?", cached, card, con, tables)
    assert inner.calls == 2 and result.answer.kind == "cannot_answer"
    cache.close()

    failing = ScriptedProvider([InvalidLLMOutput("a"), InvalidLLMOutput("b")])
    result = run("b0", "What was the ROAS?", failing, card, con, tables)
    assert result.error == "invalid_output" and result.answer.kind == "cannot_answer"
    assert result.llm_calls == 2


# --- B1 ---------------------------------------------------------------------------------


def test_b1_recovers_from_a_bad_column(
    questions: dict[str, Question], card: str, con: Any, tables: set[str]
) -> None:
    q = questions["LK01"]
    good = ref_sql(q)
    bad = good.replace("SUM(f.leads)", "SUM(f.leadz)")
    assert bad != good

    def script(req: LLMRequest) -> dict[str, Any]:
        text = req.messages[-1].text
        empty = {"thought": "", "action": "run_sql", "final": answer([])}
        if "(none yet)" in text:
            return {**empty, "thought": "Sum the leads.", "sql": bad}
        if "Step 2:" not in text:
            assert "Error:" in text and "leadz" in text
            return {**empty, "thought": "Fix the column.", "sql": good}
        return {
            "thought": "Done.",
            "action": "final",
            "sql": "",
            "final": answer(first_row_values(text)),
        }

    provider = ScriptedProvider(script)
    result = run("b1", q.question, provider, card, con, tables)
    assert provider.calls == 3 and result.llm_calls == 3
    assert result.answer.sql == [bad, good]
    assert score(q, result.answer).passed, result.answer


def test_b1_step_limit(card: str, con: Any, tables: set[str]) -> None:
    step = {
        "thought": "Look again.",
        "action": "run_sql",
        "sql": "SELECT COUNT(*) AS n FROM ads",
        "final": answer([]),
    }
    provider = ScriptedProvider(lambda req: step)
    result = run("b1", "Keep looking", provider, card, con, tables)
    assert provider.calls == 6 and result.llm_calls == 6
    assert result.answer.kind == "cannot_answer"
    assert result.answer.summary == "step limit reached"
    assert len(result.answer.sql) == 5
    assert provider.requests[-1].messages[-1].text.endswith(B1_LAST_STEP)
    assert B1_LAST_STEP not in provider.requests[-2].messages[-1].text


def test_system_text_is_identical_across_questions(
    questions: dict[str, Question], card: str, con: Any, tables: set[str]
) -> None:
    def script(req: LLMRequest) -> dict[str, Any]:
        if req.schema_name == "B0Plan":
            return {"kind": "cannot_answer", "sql": "", "reason": "test"}
        return {"thought": "", "action": "final", "sql": "", "final": answer([])}

    provider = ScriptedProvider(script)
    for qid in ("LK01", "AG01", "TR01"):
        for arch in ("b0", "b1"):
            run(arch, questions[qid].question, provider, card, con, tables)
    systems = {r.system for r in provider.requests}
    assert len(provider.requests) == 6 and len(systems) == 1
    assert next(iter(systems)).endswith(card)
