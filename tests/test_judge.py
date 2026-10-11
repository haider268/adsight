import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from adsight.agent.context import model_tables, schema_card
from adsight.bench_run import RunConfig, RunStore, run_benchmark
from adsight.benchmark.answer import AgentAnswer
from adsight.benchmark.questions import Question, RootCauseScoring, load_questions, save_questions
from adsight.generate.context import build_context
from adsight.judge import JUDGE_SYSTEM, JudgeJSON, combine, judge_root_cause, reference_text
from adsight.llm.cache import CachedProvider, ResponseCache
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest
from adsight.semantic.model import load_model
from adsight.sql_safety import open_agent_connection

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
GOLD = REPO_ROOT / "benchmark" / "questions.jsonl"
SPEC = "fixture:scripted"


def demo(qid: str) -> Question:
    return next(q for q in load_questions(DEMO_SET / "questions.jsonl") if q.id == qid)


def summary_of(q: Question) -> str:
    assert isinstance(q.scoring, RootCauseScoring) and q.scoring.reference_summary
    return q.scoring.reference_summary


def verdict(identifies: bool, invents: bool) -> dict[str, Any]:
    return {
        "reasoning": "compared",
        "identifies_true_cause": identifies,
        "invents_unsupported_cause": invents,
    }


def test_judge_request_carries_reference_and_answer() -> None:
    provider = ScriptedProvider([verdict(True, False)])
    answer = AgentAnswer(kind="answer", summary="The budget doubled on 20 April.")
    result = asyncio.run(
        judge_root_cause(provider, SPEC, "Why did CPL rise?", answer, "Budget doubled on S13.")
    )
    assert result.passed
    req = provider.requests[0]
    assert req.system == JUDGE_SYSTEM and req.schema_name == "JudgeJSON"
    text = req.messages[-1].text
    assert "Budget doubled on S13." in text and "The budget doubled on 20 April." in text
    assert "Why did CPL rise?" in text


def test_the_judge_decides_and_the_rule_stands_in_when_off() -> None:
    def j(identifies: bool, invents: bool) -> JudgeJSON:
        return JudgeJSON.model_validate(verdict(identifies, invents))

    assert combine(True, j(True, False)) is True
    assert combine(True, j(True, True)) is False
    assert combine(True, j(False, False)) is False
    assert combine(False, j(True, False)) is True
    assert combine(True, None) is True and combine(False, None) is False


def test_judge_prompt_is_the_calibrated_text() -> None:
    assert JUDGE_SYSTEM == (
        "You grade an ads analyst's explanation of why a metric changed, against the known "
        "true cause (the reference). Write your reasoning first, then decide two things.\n"
        "identifies_true_cause: true when the explanation names the same cause as the "
        "reference: the same kind of event or condition (for example a paused ad, a budget "
        "change, an audience change, broken conversion tracking, creative fatigue, a shorter "
        "or longer month, or ordinary week-to-week variation with no logged change) on the "
        "same entity or one of its parents or children. A symptom is not a cause: "
        "'conversions dropped' or 'the conversion rate collapsed' does not identify broken "
        "tracking unless tracking or measurement is named as the explanation. When the "
        "reference says nothing changed and the movement is normal variation, an explanation "
        "that says no logged change explains it and that the movement came from the "
        "conversion rate or a short-lived spike while spend and clicks were steady does "
        "identify the cause. When the reference is about month length, the explanation must "
        "point to the number of days or to steady spend per day.\n"
        "invents_unsupported_cause: true only when the explanation asserts, as a cause, a "
        "different event or condition that the reference does not contain (for example "
        "blaming a budget change when the reference says the month was shorter). Describing "
        "how the true cause worked (for example diminishing returns after a budget increase, "
        "or a lower conversion rate), quoting numbers, ruling a cause out, or mentioning a "
        "possibility alongside the true cause is not inventing."
    )


def test_second_identical_judge_call_is_a_cache_hit(tmp_path: Path) -> None:
    inner = ScriptedProvider([verdict(True, False)])
    cache = ResponseCache(tmp_path / "cache.sqlite")
    provider = CachedProvider(inner, cache)
    answer = AgentAnswer(kind="answer", summary="x")
    for _ in range(2):
        asyncio.run(judge_root_cause(provider, SPEC, "q", answer, "ref"))
    assert inner.calls == 1 and provider.hits == 1
    cache.close()


def test_invalid_judge_output_is_retried_then_raises() -> None:
    from adsight.llm.base import InvalidLLMOutput

    provider = ScriptedProvider([{"wrong": 1}, verdict(False, False)])
    answer = AgentAnswer(kind="answer", summary="x")
    assert not asyncio.run(judge_root_cause(provider, SPEC, "q", answer, "ref")).passed
    failing = ScriptedProvider([{"wrong": 1}, InvalidLLMOutput("nope")])
    with pytest.raises(InvalidLLMOutput):
        asyncio.run(judge_root_cause(failing, SPEC, "q", answer, "ref"))


def test_reference_text(bench_manifest: dict[str, Any]) -> None:
    generated = demo("RC01")
    assert reference_text(generated, None) == summary_of(generated)
    gold = next(
        q
        for q in load_questions(GOLD)
        if isinstance(q.scoring, RootCauseScoring) and q.scoring.incident_ids
    )
    assert isinstance(gold.scoring, RootCauseScoring)
    text = reference_text(gold, bench_manifest)
    causes = {i["incident_id"]: i["cause"] for i in bench_manifest["incidents"]}
    assert text == " ".join(causes[i] for i in gold.scoring.incident_ids)
    assert reference_text(gold, None) is None
    assert reference_text(demo("LK01"), bench_manifest) is None


# --- the runner -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = open_agent_connection(bench_data_dir / "adsight.duckdb")
    yield c
    c.close()


@pytest.fixture(scope="module")
def card(con: duckdb.DuckDBPyConnection) -> str:
    model = load_model(DEMO_YAML)
    return schema_card(model, build_context(model, con), con)


def final_answer(summaries: dict[str, str]) -> Any:
    def script(req: LLMRequest) -> dict[str, Any]:
        text = req.messages[-1].text
        summary = next(s for q, s in summaries.items() if q in text)
        final = {
            "kind": "answer",
            "summary": summary,
            "values": [],
            "entities": [],
            "direction": "none",
            "assumptions": [],
        }
        return {"thought": "", "action": "final", "sql": "", "final": final}

    return script


def test_runner_two_layers_and_off(card: str, con: Any, tmp_path: Path) -> None:
    rc01, rc02 = demo("RC01"), demo("RC02")
    qdir = tmp_path / "rc"
    save_questions([rc01, rc02], qdir / "questions.jsonl")
    summaries = {rc01.question: summary_of(rc01), rc02.question: summary_of(rc02)}
    store = RunStore(tmp_path / "runs.sqlite")
    tables = set(model_tables(load_model(DEMO_YAML)))

    def go(cfg: RunConfig, judge: Any) -> Any:
        agent = ScriptedProvider(final_answer(summaries))
        return asyncio.run(
            run_benchmark(
                cfg, agent, card, con, tables, store, tmp_path / "reports", judge_provider=judge
            )
        )

    off = go(RunConfig(qdir, "b1", SPEC, run_id="off"), None)
    assert off.report["judge"] == "off"
    rows = {r["question_id"]: r for r in store.results("off")}
    assert all(r["passed"] == 1 and r["judge_passed"] is None for r in rows.values())

    def judge_script(req: LLMRequest) -> dict[str, Any]:
        invents = rc02.question in req.messages[-1].text
        return verdict(True, invents)

    judge = ScriptedProvider(judge_script)
    on = go(RunConfig(qdir, "b1", SPEC, run_id="on", judge=SPEC), judge)
    assert judge.calls == 2 and on.report["judge"] == SPEC
    rows = {r["question_id"]: r for r in store.results("on")}
    assert rows["RC01"]["passed"] == 1 and rows["RC01"]["judge_passed"] == 1
    assert rows["RC02"]["passed"] == 0 and rows["RC02"]["judge_passed"] == 0
    assert "judge: invents a cause" in rows["RC02"]["reason"]
    info = json.loads(rows["RC02"]["judge_json"])
    assert info["rule_passed"] is True and info["invents_unsupported_cause"] is True
    assert on.report["per_category"]["root_cause"] == {
        "passed": 1,
        "total": 2,
        "judged": 2,
        "judge_passed": 1,
        "rule_passed": 2,
    }
    assert rows["RC01"]["rule_passed"] == 1 and rows["RC02"]["rule_passed"] == 1

    # The judge decides: a summary the rule rejects passes when the judge accepts it.
    summaries[rc01.question] = "Something about the account changed."
    accepting = ScriptedProvider(lambda req: verdict(True, False))
    judged = go(RunConfig(qdir, "b1", SPEC, run_id="judged", judge=SPEC), accepting)
    rows = {r["question_id"]: r for r in store.results("judged")}
    assert rows["RC01"]["passed"] == 1 and rows["RC01"]["rule_passed"] == 0
    assert rows["RC01"]["reason"].startswith("rule check: summary does not mention")
    assert judged.report["per_category"]["root_cause"]["rule_passed"] == 1
    answers = {a["question_id"]: a for a in judged.report["answers"]}
    assert answers["RC01"] == {
        "question_id": "RC01",
        "category": "root_cause",
        "passed": True,
        "rule_passed": False,
        "judge_identifies": True,
        "judge_invents": False,
        "kind": "answer",
        "values": 0,
    }
    store.close()


def test_old_runs_database_gains_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    old = sqlite3.connect(str(path))
    old.execute(
        "CREATE TABLE results (run_id TEXT, question_id TEXT, status TEXT, passed INTEGER, "
        "reason TEXT, answer_json TEXT, llm_calls INTEGER, input_tokens INTEGER, "
        "output_tokens INTEGER, cost_usd REAL, latency_ms REAL, error TEXT, finished_at TEXT, "
        "PRIMARY KEY (run_id, question_id))"
    )
    old.close()
    store = RunStore(path)
    store.save("r", "LK01", "done", passed=True, judge_passed=False, untraceable_numbers=2)
    row = store.results("r")[0]
    assert row["judge_passed"] == 0 and row["untraceable_numbers"] == 2
    store.close()
