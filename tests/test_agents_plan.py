import asyncio
import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

import adsight.agent.diagnose as diagnose_module
from adsight.agent.checks import check_result
from adsight.agent.context import model_tables, schema_card
from adsight.agent.numbers import numbers_in, untraceable_numbers
from adsight.agent.schemas import AnswerJSON
from adsight.architectures import MAX_LLM_CALLS, RunResult, run_question
from adsight.benchmark.questions import Question, load_questions
from adsight.benchmark.score import score
from adsight.generate.context import build_context
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest
from adsight.semantic.model import SemanticModel, load_model
from adsight.sql_safety import SqlResult, open_agent_connection

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
SPEC = "fixture:scripted"
JOINS = (
    "FROM daily_ad_metrics f LEFT JOIN ads ad ON ad.ad_id = f.ad_id "
    "LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id "
)
S13_SQL = (
    "SELECT SUM(f.spend) AS spend, SUM(f.leads) AS leads "
    + JOINS
    + "WHERE ad_set.name = 'Homeowners 35-65' "
    "AND f.date BETWEEN DATE '2026-04-13' AND DATE '2026-04-26'"
)
WHY = 'Why did the cost per lead of the ad set "Homeowners 35-65" rise after 20 April 2026?'


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
def lk01() -> Question:
    q = next(q for q in load_questions(DEMO_SET / "questions.jsonl") if q.id == "LK01")
    return q


def ref_sql(q: Question) -> str:
    assert q.reference_sql is not None
    return (DEMO_SET / "reference_sql" / q.reference_sql).read_text(encoding="utf-8")


def first_step_values(text: str) -> list[dict[str, Any]]:
    """Read the first result row of step 1 from a prompt."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Columns: "))
    columns = lines[start][len("Columns: ") :].split(" | ")
    row = lines[start + 1].split(" | ")
    return [{"label": c, "value": float(v)} for c, v in zip(columns, row, strict=True)]


def answer(values: list[dict[str, Any]], summary: str = "From the results.") -> dict[str, Any]:
    return {
        "kind": "answer",
        "summary": summary,
        "values": values,
        "entities": [],
        "direction": "none",
        "assumptions": [],
    }


def plan(*sqls: str) -> dict[str, Any]:
    return {
        "kind": "query",
        "reason": "",
        "steps": [{"goal": f"step {i}", "sql": s} for i, s in enumerate(sqls, 1)],
    }


Script = Callable[[LLMRequest], dict[str, Any]]


def by_schema(**replies: Any) -> Script:
    """Reply by schema name; a list value is consumed in order."""
    queues = {k: list(v) if isinstance(v, list) else v for k, v in replies.items()}

    def script(req: LLMRequest) -> dict[str, Any]:
        reply = queues[req.schema_name]
        if isinstance(reply, list):
            reply = reply.pop(0)
        result: dict[str, Any] = reply(req) if callable(reply) else reply
        return result

    return script


def run(arch: str, question: str, script: Script, card: str, con: Any, model: Any) -> Any:
    provider = ScriptedProvider(script)
    result: RunResult = asyncio.run(
        run_question(
            arch, question, provider, SPEC, card, con, set(model_tables(model)), semantic=model
        )
    )
    return result, provider


def test_max_calls_table() -> None:
    assert MAX_LLM_CALLS == {"b0": 2, "b1": 6, "b2": 2, "b3": 5, "b4": 7}


# --- B2 -----------------------------------------------------------------------------------


def test_b2_two_steps_then_answer(lk01: Question, card: str, con: Any, model: Any) -> None:
    script = by_schema(
        PlanJSON=plan(ref_sql(lk01), "SELECT COUNT(*) AS ads FROM ads"),
        AnswerJSON=lambda req: answer(first_step_values(req.messages[-1].text)),
    )
    result, provider = run("b2", lk01.question, script, card, con, model)
    assert provider.calls == 2 and result.llm_calls == 2
    assert len(result.answer.sql) == 2
    assert score(lk01, result.answer).passed, result.answer


def test_b2_never_fixes(card: str, con: Any, model: Any) -> None:
    script = by_schema(
        PlanJSON=plan("SELECT SUM(f.leadz) AS leads FROM daily_ad_metrics f"),
        AnswerJSON=answer([], "The query failed."),
    )
    _, provider = run("b2", "How many leads?", script, card, con, model)
    assert provider.calls == 2
    assert [r.schema_name for r in provider.requests] == ["PlanJSON", "AnswerJSON"]
    assert "The query failed" in provider.requests[1].messages[-1].text


def test_b2_clarify_plan_finishes_directly(card: str, con: Any, model: Any) -> None:
    script = by_schema(PlanJSON={"kind": "clarify", "reason": "Which account?", "steps": []})
    result, provider = run("b2", "How did we do?", script, card, con, model)
    assert provider.calls == 1 and result.answer.kind == "clarify"


# --- B3 -----------------------------------------------------------------------------------


def test_b3_fixes_a_bad_column(lk01: Question, card: str, con: Any, model: Any) -> None:
    good = ref_sql(lk01)
    bad = good.replace("SUM(f.leads)", "SUM(f.leadz)")
    script = by_schema(
        PlanJSON=plan(bad),
        FixJSON={"diagnosis": "leadz is not a column; it is leads.", "sql": good},
        AnswerJSON=lambda req: answer(first_step_values(req.messages[-1].text)),
    )
    result, provider = run("b3", lk01.question, script, card, con, model)
    assert provider.calls == 3 and result.llm_calls == 3
    assert "leadz" in provider.requests[1].messages[-1].text
    assert result.answer.sql == [bad, good]
    assert score(lk01, result.answer).passed, result.answer


def test_b3_stops_after_three_fixes(card: str, con: Any, model: Any) -> None:
    bad = "SELECT SUM(f.leadz) AS leads FROM daily_ad_metrics f"
    script = by_schema(
        PlanJSON=plan(bad),
        FixJSON={"diagnosis": "try again", "sql": bad},
        AnswerJSON=answer([], "No valid result."),
    )
    result, provider = run("b3", "How many leads?", script, card, con, model)
    names = [r.schema_name for r in provider.requests]
    assert names == ["PlanJSON", "FixJSON", "FixJSON", "FixJSON", "AnswerJSON"]
    assert result.llm_calls == 5 and len(result.answer.sql) == 4


# --- B4 -----------------------------------------------------------------------------------


def test_b4_ambiguous_and_unanswerable(card: str, con: Any, model: Any) -> None:
    ambiguous = by_schema(ClassifyJSON={"kind": "ambiguous", "reason": "Which metric?"})
    result, provider = run("b4", "How are we doing?", ambiguous, card, con, model)
    assert provider.calls == 1 and result.answer.kind == "clarify"
    assert result.answer.summary == "Which metric?" and result.answer.sql == []
    refuse = by_schema(ClassifyJSON={"kind": "unanswerable", "reason": "No revenue data."})
    result, provider = run("b4", "What was the ROAS?", refuse, card, con, model)
    assert provider.calls == 1 and result.answer.kind == "cannot_answer"
    assert result.answer.sql == []


def test_b4_diagnoses_the_s13_budget_change(card: str, con: Any, model: Any) -> None:
    seen: list[str] = []

    def synthesize(req: LLMRequest) -> dict[str, Any]:
        seen.append(req.messages[-1].text)
        return answer(
            [],
            "The ad set S13 budget doubled from 55.00 to 110.00 on 20 April 2026, and its "
            "spend rose 96.5% in the 14 days from the change.",
        )

    script = by_schema(
        ClassifyJSON={"kind": "diagnostic", "reason": "why question"},
        PlanJSON=plan(S13_SQL),
        AnswerJSON=synthesize,
    )
    result, provider = run("b4", WHY, script, card, con, model)
    assert [r.schema_name for r in provider.requests] == ["ClassifyJSON", "PlanJSON", "AnswerJSON"]
    assert "budget_change 55.00 -> 110.00" in seen[0]
    assert "2026-04-20 ad_set S13" in seen[0]
    assert result.untraceable_numbers == 0
    assert result.answer.sql == [S13_SQL]


def test_b4_resynthesizes_untraceable_numbers(card: str, con: Any, model: Any) -> None:
    script = by_schema(
        ClassifyJSON={"kind": "diagnostic", "reason": "why question"},
        PlanJSON=plan(S13_SQL),
        AnswerJSON=[
            answer([], "Cost per lead rose 42.7% after the budget went to 110.00."),
            answer([], "Cost per lead rose after the budget went from 55.00 to 110.00."),
        ],
    )
    result, provider = run("b4", WHY, script, card, con, model)
    names = [r.schema_name for r in provider.requests]
    assert names == ["ClassifyJSON", "PlanJSON", "AnswerJSON", "AnswerJSON"]
    assert "42.7" in provider.requests[-1].messages[-1].text
    assert result.untraceable_numbers == 0
    assert "55.00" in result.answer.summary


def test_b4_reports_numbers_still_untraceable(card: str, con: Any, model: Any) -> None:
    made_up = answer([{"label": "cpl", "value": 99.99}], "CPL was 99.99.")
    script = by_schema(
        ClassifyJSON={"kind": "lookup", "reason": "one number"},
        PlanJSON=plan(S13_SQL),
        AnswerJSON=[made_up, made_up],
    )
    result, provider = run("b4", WHY, script, card, con, model)
    assert provider.calls == 4
    assert result.untraceable_numbers == 1


def test_diagnose_never_touches_the_generator() -> None:
    source = inspect.getsource(diagnose_module)
    assert "adsight.generate" not in source


def test_diagnose_says_when_it_cannot_tell(model: SemanticModel, con: Any) -> None:
    from adsight.agent.diagnose import diagnose

    no_entity = diagnose(model, con, "Why did leads fall in April 2026?", [])
    assert no_entity == "Diagnosis: cannot determine which entity the question is about."
    no_window = diagnose(
        model, con, 'Why did "Homeowners 35-65" get worse?', [{"sql": "SELECT 'Homeowners 35-65'"}]
    )
    assert no_window == "Diagnosis: cannot determine the date window the question is about."


# --- number verification ------------------------------------------------------------------


def test_untraceable_numbers_ignores_years_dates_and_the_question() -> None:
    a = AnswerJSON.model_validate(
        answer(
            [{"label": "cpl", "value": 12.07}],
            "On 20 April 2026 (2026-04-20) and April 3 the 7 ad sets had CPL 12.07, or 4.5%.",
        )
    )
    assert untraceable_numbers(a, "Which 7 ad sets?", "cpl 12.07 ctr 0.045") == []
    bad = AnswerJSON.model_validate(answer([], "CPL rose 31%."))
    assert untraceable_numbers(bad, "Why?", "cpl 12.07 -> 15.81 (+31.0%)") == []
    assert untraceable_numbers(bad, "Why?", "cpl 12.07 -> 15.81") == [31.0]
    assert numbers_in("1,234.5 and 7") == [1234.5, 7.0]


# --- checks -------------------------------------------------------------------------------


def result(columns: list[str], rows: list[tuple[Any, ...]], error: str | None = None) -> Any:
    return SqlResult(columns=columns, rows=rows, row_count=len(rows), error=error)


def test_check_rules() -> None:
    fine = result(["spend", "cpl", "ctr", "ctr_pct"], [(10.0, 12.5, 0.02, 2.0)])
    assert check_result("g", fine) == []
    assert check_result("g", result([], [], "Binder Error: no column"))[0].startswith(
        "The query failed"
    )
    assert check_result("g", result(["a"], [])) == ["The query returned no rows."]
    assert check_result("g", result(["a", "b"], [(None, 1), (None, 2)])) == [
        "Column a is NULL in every row."
    ]
    assert check_result("g", result(["total_cost"], [(-5.0,)])) == [
        "Column total_cost has a negative value."
    ]
    assert check_result("g", result(["cost_per_lead"], [(25000.0,)])) == [
        "Column cost_per_lead is above 10,000, too high for a cost ratio."
    ]
    assert check_result("g", result(["cvr"], [(4.5,)])) == [
        "Column cvr is above 1.0, but a rate should be a fraction."
    ]


def test_impossible_dates_are_skipped() -> None:
    from datetime import date

    from adsight.agent.diagnose import find_window

    sql = (
        "SELECT 1 FROM daily_ad_metrics f WHERE f.date BETWEEN DATE '2026-02-30' "
        "AND DATE '2026-04-31' OR f.date BETWEEN DATE '2026-02-10' AND DATE '2026-03-05'"
    )
    assert find_window("Why?", [{"sql": sql}]) == (date(2026, 2, 10), date(2026, 3, 5))
    # the impossible day is skipped; the month it names is still a valid window
    assert find_window("What happened on 30 February 2026?", []) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )


def test_diagnose_never_raises(
    model: SemanticModel, con: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adsight.agent.diagnose import diagnose

    def boom(*args: Any) -> Any:
        raise RuntimeError("crafted failure")

    monkeypatch.setattr(diagnose_module, "_entities", boom)
    steps = [{"sql": S13_SQL, "rows": []}]
    assert diagnose(model, con, WHY, steps) == "Diagnosis unavailable: RuntimeError"
