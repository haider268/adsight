import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from adsight.bench_run import (
    EXIT_OK,
    EXIT_STOPPED,
    EXIT_WAITING,
    RunConfig,
    RunStore,
    load_run_questions,
    run_benchmark,
    stratified_sample,
)
from adsight.benchmark.questions import load_questions, save_questions
from adsight.cli import app
from adsight.config import Settings
from adsight.llm.cache import ResponseCache
from adsight.llm.factory import ProviderPool
from adsight.llm.types import LLMRequest
from adsight.plans import (
    Plan,
    export_plan,
    load_plan,
    plan_settings,
    record_progress,
    resume_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
PLANS = REPO_ROOT / "benchmark" / "plans"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bench-tick.yml"
HAIKU = "anthropic:claude-haiku-4-5"
COUNT_SQL = "SELECT COUNT(*) AS n FROM ads"

runner = CliRunner()


def demo_questions(*ids: str) -> list[Any]:
    by_id = {q.id: q for q in load_questions(DEMO_SET / "questions.jsonl")}
    return [by_id[i] for i in ids]


def qdir_with(tmp_path: Path, name: str, *ids: str) -> Path:
    folder = tmp_path / name
    save_questions(demo_questions(*ids), folder / "questions.jsonl")
    return folder


def settings_for(tmp_path: Path, budget: float = 10.0) -> Settings:
    return Settings(
        _env_file=None,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "nodata",
        anthropic_budget_usd=budget,
    )


# --- plans -------------------------------------------------------------------------------


def job(**overrides: Any) -> dict[str, Any]:
    base = {"id": "j1", "questions_dir": "x", "arch": "b0", "model": HAIKU}
    return base | overrides


def test_plan_validation() -> None:
    Plan.model_validate({"name": "p", "budget_usd": 1, "jobs": [job()]})
    with pytest.raises(ValidationError, match="unknown arch"):
        Plan.model_validate({"name": "p", "budget_usd": 1, "jobs": [job(arch="b9")]})
    with pytest.raises(ValidationError, match="duplicate job ids: j1"):
        Plan.model_validate({"name": "p", "budget_usd": 1, "jobs": [job(), job()]})
    with pytest.raises(ValidationError):
        Plan.model_validate({"name": "p", "budget_usd": 1, "jobs": [job(model="nope")]})
    with pytest.raises(ValidationError):
        Plan.model_validate({"name": "p", "budget_usd": 1, "jobs": []})


def test_committed_plans(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    demo = load_plan(PLANS / "haiku-demo.yaml")
    assert demo.name == "haiku-demo" and demo.budget_usd == 25
    assert [j.id for j in demo.jobs] == [
        "demo-b0",
        "demo-b1",
        "demo-b2",
        "demo-b3",
        "demo-b4",
        "gold-b0",
        "gold-b4",
        "var-b4-1",
        "var-b4-2",
    ]
    assert all(j.batch and j.model == HAIKU and j.judge == HAIKU for j in demo.jobs)
    sizes = {j.id: len(load_run_questions(j.config(settings))) for j in demo.jobs}
    assert sizes["demo-b0"] == 99 and sizes["gold-b4"] == 100 and sizes["var-b4-1"] == 20
    by_id = {j.id: j for j in demo.jobs}
    assert (by_id["var-b4-1"].salt, by_id["var-b4-2"].salt) == ("rep1", "rep2")
    same = [
        [q.id for q in load_run_questions(by_id[i].config(settings))]
        for i in ("var-b4-1", "var-b4-2")
    ]
    assert same[0] == same[1]
    pilot = load_plan(PLANS / "haiku-pilot.yaml")
    assert [j.arch for j in pilot.jobs] == ["b0", "b1", "b2", "b3", "b4"]
    assert all(j.sample == 10 and j.batch and j.judge == HAIKU for j in pilot.jobs)
    assert plan_settings(demo, settings).anthropic_budget_usd == 10.0
    assert plan_settings(demo, settings_for(tmp_path, 100)).anthropic_budget_usd == 25


def test_stratified_sample_spreads_over_categories() -> None:
    questions = load_questions(DEMO_SET / "questions.jsonl")
    picked = stratified_sample(questions, 14, seed=7)
    assert len(picked) == 14
    assert set(Counter(q.category for q in picked).values()) == {2}
    assert [q.id for q in picked] == [q.id for q in stratified_sample(questions, 14, seed=7)]
    assert [q.id for q in picked] != [q.id for q in stratified_sample(questions, 14, seed=8)]


# --- a fake Anthropic batch API -----------------------------------------------------------


def _reply(params: dict[str, Any]) -> dict[str, Any]:
    props = params["output_config"]["format"]["schema"]["properties"]
    if "identifies_true_cause" in props:
        return {"reasoning": "", "identifies_true_cause": True, "invents_unsupported_cause": False}
    if "steps" in props:
        return {"kind": "query", "reason": "", "steps": [{"goal": "count", "sql": COUNT_SQL}]}
    if "sql" in props and "reason" in props:
        return {"kind": "query", "sql": COUNT_SQL, "reason": ""}
    return {
        "kind": "answer",
        "summary": "Counted.",
        "values": [],
        "entities": [],
        "direction": "none",
        "assumptions": [],
    }


def _message(params: dict[str, Any]) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(_reply(params)))],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class FakeBatches:
    def __init__(self, auto_end: bool = True):
        self.auto_end = auto_end
        self.created: list[tuple[str, list[dict[str, Any]]]] = []

    async def create(self, requests: list[dict[str, Any]]) -> Any:
        batch_id = f"msgbatch_{len(self.created) + 1:03d}"
        self.created.append((batch_id, list(requests)))
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    async def retrieve(self, batch_id: str) -> Any:
        status = "ended" if self.auto_end else "in_progress"
        return SimpleNamespace(id=batch_id, processing_status=status, request_counts=None)

    async def results(self, batch_id: str) -> Any:
        requests = dict(self.created)[batch_id]

        async def gen() -> Any:
            for r in reversed(requests):
                result = SimpleNamespace(type="succeeded", message=_message(r["params"]))
                yield SimpleNamespace(custom_id=r["custom_id"], result=result)

        return gen()


def fake_client(batches: FakeBatches) -> Any:
    return SimpleNamespace(messages=SimpleNamespace(batches=batches))


FAST = {"idle_flush_s": 0.05, "poll_s": 0.02}


def pool_for(settings: Settings, batches: FakeBatches, wait_s: float | None = None) -> Any:
    return ProviderPool(
        settings, True, client=fake_client(batches), wait_s=wait_s, batch_options=FAST
    )


def card_and_con(bench_data_dir: Path) -> Any:
    from adsight.agent.context import model_tables, schema_card
    from adsight.generate.context import build_context
    from adsight.semantic.model import load_model
    from adsight.sql_safety import open_agent_connection

    model = load_model(DEMO_YAML)
    con = open_agent_connection(bench_data_dir / "adsight.duckdb")
    return schema_card(model, build_context(model, con), con), con, set(model_tables(model))


def kinds(requests: list[dict[str, Any]]) -> list[str]:
    out = []
    for r in requests:
        props = r["params"]["output_config"]["format"]["schema"]["properties"]
        out.append("answer" if "summary" in props else "plan")
    return out


def test_tick_mode_stops_waiting_then_resumes_from_the_cache(
    tmp_path: Path, bench_data_dir: Path
) -> None:
    settings = settings_for(tmp_path)
    qdir = qdir_with(tmp_path, "two", "LK01", "LK02")
    cfg = RunConfig(qdir, "b0", HAIKU, batch=True, run_id="tick")
    card, con, tables = card_and_con(bench_data_dir)
    store = RunStore(settings.runs_db_path)
    batches = FakeBatches(auto_end=False)
    try:
        first_pool = pool_for(settings, batches, wait_s=0.2)
        first = asyncio.run(
            run_benchmark(cfg, first_pool.get(HAIKU), card, con, tables, store, tmp_path / "r")
        )
        first_pool.close()
        assert first.exit_code == EXIT_WAITING
        assert {r["status"] for r in store.results("tick")} == {"retry"}
        assert len(batches.created) == 1 and kinds(batches.created[0][1]) == ["plan", "plan"]

        batches.auto_end = True  # the batch ends while the laptop is off
        second_pool = pool_for(settings, batches)
        second = asyncio.run(
            run_benchmark(cfg, second_pool.get(HAIKU), card, con, tables, store, tmp_path / "r")
        )
        open_after = second_pool.cache.open_batches()
        second_pool.close()
    finally:
        con.close()
    assert second.exit_code == EXIT_OK
    assert {r["status"] for r in store.results("tick")} == {"done"}
    assert len(batches.created) == 2
    assert kinds(batches.created[1][1]) == ["answer", "answer"]
    ids = [r["custom_id"] for _, reqs in batches.created for r in reqs]
    assert len(ids) == len(set(ids))
    assert open_after == []
    store.close()


def write_plan(tmp_path: Path, jobs: list[dict[str, Any]], budget: float = 25) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump({"name": "t", "budget_usd": budget, "jobs": jobs}))
    return path


def test_resume_runs_jobs_concurrently_in_one_batch(tmp_path: Path, bench_data_dir: Path) -> None:
    qdir = qdir_with(tmp_path, "one", "LK01")
    db = str(bench_data_dir / "adsight.duckdb")
    common = {"questions_dir": str(qdir), "semantic": str(DEMO_YAML), "db": db}
    jobs = [
        {"id": "a", "arch": "b0", "model": HAIKU, "batch": True, **common},
        {"id": "b", "arch": "b2", "model": HAIKU, "batch": True, **common},
    ]
    plan = load_plan(write_plan(tmp_path, jobs))
    settings = plan_settings(plan, settings_for(tmp_path))
    batches = FakeBatches()
    pool = pool_for(settings, batches)
    store = RunStore(settings.runs_db_path)
    tick = asyncio.run(resume_plan(plan, settings, pool, store))
    pool.close()
    assert tick.exit_code == EXIT_OK
    assert [(p.job, p.done, p.total) for p in tick.progress] == [("a", 1, 1), ("b", 1, 1)]
    assert [len(r) for _, r in batches.created] == [2, 2]  # plan wave, answer wave
    assert tick.spent_usd > 0 and tick.batches_in_flight == 0
    record_progress(plan, tick, settings)
    again = asyncio.run(resume_plan(plan, settings, pool_for(settings, batches), store))
    assert again.exit_code == EXIT_OK and len(batches.created) == 2
    record_progress(plan, again, settings)
    lines = (settings.state_dir / "progress.log").read_text().splitlines()
    assert len(lines) == 4 and "job=a done=1/1" in lines[0]
    store.close()


def test_budget_stop_exits_3(
    tmp_path: Path, bench_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdir = qdir_with(tmp_path, "one", "LK01")
    jobs = [
        {
            "id": "a",
            "arch": "b0",
            "model": HAIKU,
            "batch": True,
            "questions_dir": str(qdir),
            "semantic": str(DEMO_YAML),
            "db": str(bench_data_dir / "adsight.duckdb"),
        }
    ]
    plan = load_plan(write_plan(tmp_path, jobs))
    settings = plan_settings(plan, settings_for(tmp_path, budget=0.0))
    batches = FakeBatches()
    store = RunStore(settings.runs_db_path)
    tick = asyncio.run(resume_plan(plan, settings, pool_for(settings, batches), store))
    assert tick.exit_code == EXIT_STOPPED and batches.created == []
    assert tick.reason is not None and "budget" in tick.reason
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    record_progress(plan, tick, settings)
    assert "| a | 0/1 | 1 | 0 |" in summary.read_text()
    store.close()


def test_cli_resume_budget_stop(
    tmp_path: Path, bench_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdir = qdir_with(tmp_path, "one", "LK01")
    jobs = [
        {
            "id": "a",
            "arch": "b0",
            "model": HAIKU,
            "batch": True,
            "questions_dir": str(qdir),
            "semantic": str(DEMO_YAML),
            "db": str(bench_data_dir / "adsight.duckdb"),
        }
    ]
    plan_path = write_plan(tmp_path, jobs)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("ADSIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ADSIGHT_ANTHROPIC_BUDGET_USD", "0")
    result = runner.invoke(app, ["bench", "resume", "--plan", str(plan_path)])
    assert result.exit_code == EXIT_STOPPED, result.output
    assert (tmp_path / "state" / "progress.log").read_text().count("job=a") == 1


# --- salt, export and replay --------------------------------------------------------------


def scripted(req: LLMRequest) -> dict[str, Any]:
    if req.schema_name == "JudgeJSON":
        return {"reasoning": "", "identifies_true_cause": True, "invents_unsupported_cause": False}
    final = {
        "kind": "answer",
        "summary": "From the scripted agent.",
        "values": [{"label": "n", "value": 50.0}],
        "entities": [],
        "direction": "none",
        "assumptions": [],
    }
    return {"thought": "", "action": "final", "sql": "", "final": final}


def test_salted_runs_are_independent(tmp_path: Path, bench_data_dir: Path) -> None:
    settings = settings_for(tmp_path)
    qdir = qdir_with(tmp_path, "one", "LK01")
    card, con, tables = card_and_con(bench_data_dir)
    store = RunStore(settings.runs_db_path)
    pool = ProviderPool(settings, script=scripted)
    provider = pool.get("fixture:scripted")
    for run_id, salt in (("plain", ""), ("same", ""), ("rep1", "rep1")):
        cfg = RunConfig(qdir, "b1", "fixture:scripted", run_id=run_id, salt=salt)
        asyncio.run(run_benchmark(cfg, provider, card, con, tables, store, tmp_path / "r"))
    con.close()
    assert provider.misses == 2 and provider.hits == 1
    assert store.call_keys("plain") == store.call_keys("same") != store.call_keys("rep1")
    pool.close()
    store.close()


def test_export_wipe_and_replay_from_the_cassette(tmp_path: Path, bench_data_dir: Path) -> None:
    qdir = qdir_with(tmp_path, "two", "LK01", "RC01")
    jobs = [
        {
            "id": "fx",
            "arch": "b1",
            "model": "fixture:scripted",
            "judge": "fixture:scripted",
            "questions_dir": str(qdir),
            "semantic": str(DEMO_YAML),
            "db": str(bench_data_dir / "adsight.duckdb"),
        }
    ]
    plan = load_plan(write_plan(tmp_path, jobs))
    settings = plan_settings(plan, settings_for(tmp_path))
    store = RunStore(settings.runs_db_path)
    pool = ProviderPool(settings, script=scripted)
    tick = asyncio.run(resume_plan(plan, settings, pool, store))
    assert tick.exit_code == EXIT_OK
    out = tmp_path / "results" / "t"
    summary = export_plan(plan, settings, store, pool.cache, out)
    pool.close()
    original = {r["question_id"]: r for r in store.results("fx")}
    store.close()
    assert (out / "fx" / "report.json").is_file() and (out / "summary.json").is_file()
    assert summary["jobs"]["fx"]["categories"]["root_cause"]["judge_accuracy"] == 1.0
    assert summary["jobs"]["fx"]["categories"]["lookup"]["questions"] == 1

    settings.llm_cache_path.unlink()
    replay_store = RunStore(tmp_path / "replay.sqlite")
    replay_pool = ProviderPool(
        settings,
        mode="replay",
        script=[],
        cache=ResponseCache.from_cassette(out / "fx" / "cassette.jsonl.gz"),
    )
    card, con, tables = card_and_con(bench_data_dir)
    cfg = plan.jobs[0].config(settings)
    outcome = asyncio.run(
        run_benchmark(
            cfg,
            replay_pool.get(cfg.model_spec),
            card,
            con,
            tables,
            replay_store,
            tmp_path / "r",
            judge_provider=replay_pool.get(cfg.judge),
        )
    )
    con.close()
    replay_pool.close()
    assert outcome.exit_code == EXIT_OK
    replayed = {r["question_id"]: r for r in replay_store.results("fx")}
    for qid, row in original.items():
        for column in ("status", "passed", "answer_json", "judge_passed", "judge_json"):
            assert replayed[qid][column] == row[column], (qid, column)
    replay_store.close()


def test_cli_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [{"id": "fx", "arch": "b0", "model": "fixture:scripted", "questions_dir": str(DEMO_SET)}]
    plan_path = write_plan(tmp_path, jobs)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADSIGHT_STATE_DIR", str(tmp_path / "state"))
    result = runner.invoke(app, ["bench", "export", "--plan", str(plan_path), "--out", "res"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "res" / "fx" / "cassette.jsonl.gz").is_file()
    report = json.loads((tmp_path / "res" / "fx" / "report.json").read_text())
    assert report["status"] == "not started" and report["questions"] == 99


# --- the scheduled workflow ------------------------------------------------------------------


def test_workflow_guards() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    triggers = data.get("on", data.get(True))
    assert triggers["schedule"] == [{"cron": "*/30 * * * *"}]
    default = triggers["workflow_dispatch"]["inputs"]["plan"]["default"]
    assert default == "benchmark/plans/haiku-demo-v2.yaml"
    # scheduled ticks follow the BENCH_PLAN variable, else the current plan
    plan_env = data["jobs"]["tick"]["env"]["PLAN"]
    assert "vars.BENCH_PLAN" in plan_env and "haiku-demo-v2.yaml" in plan_env
    assert data["concurrency"] == {"group": "bench-tick", "cancel-in-progress": False}
    tick = data["jobs"]["tick"]
    assert tick["if"] == "vars.BENCH_ENABLED == 'true'"
    assert tick["timeout-minutes"] == 30 and tick["runs-on"] == "ubuntu-latest"
    steps = {s.get("name", s.get("uses")): s for s in tick["steps"]}
    run = steps["Run one tick"]
    assert "--wait-minutes 20" in run["run"]
    for code in ('"$code" -eq 0', '"$code" -eq 3', '"$code" -eq 4'):
        assert code in run["run"]
    assert run["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert run["env"]["ADSIGHT_ANTHROPIC_BUDGET_USD"] == "${{ vars.ANTHROPIC_BUDGET_USD || '0' }}"
    assert steps["Restore benchmark state"]["uses"] == "actions/cache/restore@v4"
    assert steps["Save benchmark state"]["if"] == "always()"
    assert steps["Upload reports"]["with"]["name"] == "bench-reports"
    assert steps["Upload reports"]["with"]["retention-days"] == 30
    assert "git push" not in text and "git commit" not in text


def test_retry_salt_combines_with_the_run_salt() -> None:
    from adsight.architectures.common import retry_salt

    assert retry_salt("") == "retry1"
    assert retry_salt("rep1") == "rep1:retry1"


def test_cli_cassette_needs_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    args = ["bench", "run", "--questions-dir", str(DEMO_SET), "--arch", "b0"]
    args += ["--model", "fixture:scripted", "--cassette", str(tmp_path / "c.jsonl.gz")]
    result = runner.invoke(app, args)
    assert result.exit_code == 2


def test_v2_plan_mirrors_the_first() -> None:
    first = load_plan(PLANS / "haiku-demo.yaml")
    v2 = load_plan(PLANS / "haiku-demo-v2.yaml")
    assert v2.name == "haiku-demo-v2" and len(v2.jobs) == 9
    assert all(j.id.endswith("-v2") for j in v2.jobs)
    assert [j.model_copy(update={"id": j.id[:-3]}) for j in v2.jobs] == first.jobs
    assert v2.budget_usd == first.budget_usd


def test_export_workflow_is_manual_and_secret_free() -> None:
    path = REPO_ROOT / ".github" / "workflows" / "bench-export.yml"
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    triggers = data.get("on", data.get(True))
    assert list(triggers) == ["workflow_dispatch"]
    default = triggers["workflow_dispatch"]["inputs"]["plan"]["default"]
    assert default == "benchmark/plans/haiku-demo-v2.yaml"
    steps = {s.get("name", s.get("uses")): s for s in data["jobs"]["export"]["steps"]}
    restore = steps["Restore benchmark state"]
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"]["fail-on-cache-miss"] is True
    assert restore["with"]["restore-keys"] == "bench-state-"
    assert steps["Export"]["run"] == 'uv run adsight bench export --plan "$PLAN" --out out/results'
    upload = steps["Upload results"]["with"]
    assert (upload["name"], upload["path"], upload["retention-days"]) == (
        "bench-results",
        "out/results",
        30,
    )
    assert "secrets." not in text
    assert "git push" not in text and "git commit" not in text
