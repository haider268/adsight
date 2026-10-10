"""Run plans, ticks and export: the laptop-off benchmark runner.

A plan (benchmark/plans/<name>.yaml) lists jobs. One TICK (`adsight bench resume`)
re-attaches every recorded Anthropic batch, then runs every unfinished job concurrently so
their requests share batch waves, and stops cleanly at a budget (exit 3) or after the wait
(exit 4). `export` writes each job's report and a cassette of every response it used, so
anyone can replay the published results offline.
"""

import asyncio
import gzip
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from adsight.agent.context import model_tables, schema_card
from adsight.bench_run import (
    EXIT_OK,
    EXIT_WAITING,
    RunConfig,
    RunStore,
    build_report,
    is_complete,
    load_manifest,
    load_run_questions,
    run_benchmark,
)
from adsight.config import Settings
from adsight.generate.context import build_context
from adsight.llm.cache import ResponseCache, now_iso
from adsight.llm.factory import ProviderPool, parse_spec
from adsight.semantic.model import SemanticModel, load_model
from adsight.sql_safety import open_agent_connection

ARCHS = ("b0", "b1", "b2", "b3", "b4")


class Job(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str  # also the run_id
    questions_dir: Path
    semantic: Path = Path("semantic/demo.yaml")
    db: Path | None = None  # default: <data dir>/adsight.duckdb
    arch: str
    model: str
    batch: bool = False
    judge: str = "off"
    sample: int | None = None
    salt: str = ""

    @field_validator("arch")
    @classmethod
    def _known_arch(cls, value: str) -> str:
        if value not in ARCHS:
            raise ValueError(f"unknown arch {value!r}; choose from {', '.join(ARCHS)}")
        return value

    @field_validator("model")
    @classmethod
    def _model_spec(cls, value: str) -> str:
        parse_spec(value)
        return value

    @field_validator("judge")
    @classmethod
    def _judge_spec(cls, value: str) -> str:
        if value != "off":
            parse_spec(value)
        return value

    def config(self, settings: Settings) -> RunConfig:
        return RunConfig(
            questions_dir=self.questions_dir,
            arch=self.arch,
            model_spec=self.model,
            batch=self.batch,
            run_id=self.id,
            seed=settings.seed,
            judge=self.judge,
            salt=self.salt,
            sample=self.sample,
        )


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    budget_usd: float
    jobs: list[Job]

    @model_validator(mode="after")
    def _jobs(self) -> "Plan":
        if not self.jobs:
            raise ValueError("a plan needs at least one job")
        ids = [j.id for j in self.jobs]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate job ids: {', '.join(dupes)}")
        if self.budget_usd < 0:
            raise ValueError("budget_usd must be >= 0")
        return self


def load_plan(path: Path) -> Plan:
    return Plan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def plan_settings(plan: Plan, settings: Settings) -> Settings:
    """Settings whose Anthropic budget is the lower of the plan's and the environment's."""
    budget = min(plan.budget_usd, settings.anthropic_budget_usd)
    return settings.model_copy(update={"anthropic_budget_usd": budget})


# --- progress ------------------------------------------------------------------------------


@dataclass
class JobProgress:
    job: str
    done: int
    total: int
    retry: int
    errors: int
    accuracy: float
    cost_usd: float


@dataclass
class TickResult:
    exit_code: int
    reason: str | None
    progress: list[JobProgress]
    spent_usd: float
    budget_usd: float
    batches_in_flight: int
    lines: list[str] = field(default_factory=list)


def job_progress(job: Job, settings: Settings, store: RunStore) -> JobProgress:
    cfg = job.config(settings)
    wanted = {q.id for q in load_run_questions(cfg)}
    rows = [r for r in store.results(job.id) if r["question_id"] in wanted]
    counts = Counter(r["status"] for r in rows)
    done = [r for r in rows if r["status"] == "done"]
    passed = sum(int(r["passed"] or 0) for r in done)
    return JobProgress(
        job=job.id,
        done=len(done),
        total=len(wanted),
        retry=counts.get("retry", 0),
        errors=counts.get("error", 0),
        accuracy=round(passed / len(done), 4) if done else 0.0,
        cost_usd=round(sum(r["cost_usd"] for r in rows), 6),
    )


def progress_lines(plan: Plan, progress: list[JobProgress]) -> list[str]:
    stamp = now_iso()
    return [
        f"{stamp} plan={plan.name} job={p.job} done={p.done}/{p.total} retry={p.retry} "
        f"errors={p.errors} accuracy={p.accuracy:.4f} cost_usd={p.cost_usd:.6f}"
        for p in progress
    ]


def progress_markdown(plan: Plan, tick: TickResult) -> str:
    lines = [
        f"### adsight plan {plan.name}",
        "",
        "| job | done/total | retry | errors | accuracy | cost |",
        "|---|---|---|---|---|---|",
    ]
    for p in tick.progress:
        lines.append(
            f"| {p.job} | {p.done}/{p.total} | {p.retry} | {p.errors} | "
            f"{p.accuracy:.1%} | ${p.cost_usd:.4f} |"
        )
    lines += [
        "",
        f"Spend ${tick.spent_usd:.4f} of ${tick.budget_usd:.2f}; "
        f"batches in flight {tick.batches_in_flight}; exit {tick.exit_code}.",
        "",
    ]
    return "\n".join(lines)


def record_progress(plan: Plan, tick: TickResult, settings: Settings) -> None:
    """Append one line per job to <state dir>/progress.log, and the table to the GitHub
    step summary when running in Actions."""
    log_path = settings.state_dir / "progress.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.writelines(line + "\n" for line in tick.lines)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(progress_markdown(plan, tick))


# --- one tick ------------------------------------------------------------------------------


@dataclass
class _Data:
    con: duckdb.DuckDBPyConnection
    model: SemanticModel
    card: str


async def resume_plan(
    plan: Plan, settings: Settings, pool: ProviderPool, store: RunStore
) -> TickResult:
    """One tick. `settings` should already carry the plan budget (plan_settings) and the
    pool should be built from it."""
    for job in plan.jobs:
        pool.get(job.model, job.batch)
        if job.judge != "off":
            pool.get(job.judge, job.batch)
    for batcher in pool.batchers():
        await batcher.reattach()

    data: dict[tuple[Path, Path], _Data] = {}
    stop: dict[str, Any] = {}
    manifest = load_manifest(settings.data_dir / "manifest.json")
    coros = []
    try:
        for job in plan.jobs:
            cfg = job.config(settings)
            if is_complete(store, cfg):
                continue
            db = job.db or settings.db_path
            if (job.semantic, db) not in data:
                con = open_agent_connection(db)
                model = load_model(job.semantic)
                data[(job.semantic, db)] = _Data(
                    con, model, schema_card(model, build_context(model, con), con)
                )
            d = data[(job.semantic, db)]
            coros.append(
                run_benchmark(
                    cfg,
                    pool.get(job.model, job.batch),
                    d.card,
                    d.con,
                    set(model_tables(d.model)),
                    store,
                    settings.reports_dir,
                    semantic=d.model,
                    judge_provider=None if job.judge == "off" else pool.get(job.judge, job.batch),
                    manifest=manifest,
                    stop=stop,
                )
            )
        await asyncio.gather(*coros)
    finally:
        for d in data.values():
            d.con.close()

    progress = [job_progress(job, settings, store) for job in plan.jobs]
    if stop:
        exit_code = int(stop["exit"])
    elif all(p.done == p.total for p in progress):
        exit_code = EXIT_OK
    else:
        exit_code = EXIT_WAITING  # questions left as "retry" (transient errors): tick again
    tick = TickResult(
        exit_code=exit_code,
        reason=stop.get("reason"),
        progress=progress,
        spent_usd=round(pool.cache.spent_usd("anthropic"), 6),
        budget_usd=settings.anthropic_budget_usd,
        batches_in_flight=len(pool.cache.open_batches()),
    )
    tick.lines = progress_lines(plan, progress)
    return tick


# --- export --------------------------------------------------------------------------------


def _category_summary(rows: list[dict[str, Any]], category_of: dict[str, str]) -> dict[str, Any]:
    out: dict[str, dict[str, Any]] = {}
    for category in dict.fromkeys(category_of.values()):
        done = [
            r
            for r in rows
            if r["status"] == "done" and category_of.get(r["question_id"]) == category
        ]
        n = len(done)
        judged = [r for r in done if r["judge_passed"] is not None]
        entry: dict[str, Any] = {
            "questions": n,
            "accuracy": round(sum(int(r["passed"] or 0) for r in done) / n, 4) if n else None,
            "mean_llm_calls": round(sum(r["llm_calls"] for r in done) / n, 3) if n else None,
            "mean_tokens": (
                round(sum(r["input_tokens"] + r["output_tokens"] for r in done) / n, 1)
                if n
                else None
            ),
            "cost_usd": round(
                sum(r["cost_usd"] for r in rows if category_of.get(r["question_id"]) == category),
                6,
            ),
        }
        if category == "root_cause":
            entry["judge_accuracy"] = (
                round(sum(int(r["judge_passed"]) for r in judged) / len(judged), 4)
                if judged
                else None
            )
        out[category] = entry
    return out


def write_cassette(rows: list[dict[str, Any]], path: Path) -> None:
    """Gzipped JSON lines, sorted by key, with a fixed gzip timestamp (stable bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode("utf-8")
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as fh:
        fh.write(payload)


def export_plan(
    plan: Plan, settings: Settings, store: RunStore, cache: ResponseCache, out_dir: Path
) -> dict[str, Any]:
    """Write <out>/<job>/report.json and cassette.jsonl.gz per job, and <out>/summary.json."""
    summary: dict[str, Any] = {"plan": plan.name, "jobs": {}}
    for job in plan.jobs:
        cfg = job.config(settings)
        questions = load_run_questions(cfg)
        runs = store.runs(job.id)
        status = str(runs[0]["status"]) if runs else "not started"
        report = build_report(cfg, store, questions, 0, status)
        job_dir = out_dir / job.id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        write_cassette(cache.export_rows(store.call_keys(job.id)), job_dir / "cassette.jsonl.gz")
        category_of: dict[str, str] = {q.id: str(q.category) for q in questions}
        rows = [r for r in store.results(job.id) if r["question_id"] in category_of]
        summary["jobs"][job.id] = {
            "arch": job.arch,
            "model": job.model,
            "judge": job.judge,
            "status": status,
            "categories": _category_summary(rows, category_of),
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return summary
