"""The resumable benchmark runner: every question's result is committed as it finishes.

A re-run with the same run id skips questions already "done" and re-runs the rest. A
daily limit or a spent budget stops the run cleanly: work in flight finishes, the
questions not yet run are marked "retry", and the caller exits 3.
"""

import asyncio
import json
import random
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from adsight.architectures import run_question
from adsight.benchmark.answer import AgentAnswer
from adsight.benchmark.questions import Question, effective_questions, load_questions
from adsight.benchmark.score import score
from adsight.judge import JudgeJSON, combine, judge_root_cause, reference_text
from adsight.llm.base import (
    BatchPending,
    BudgetExceeded,
    CacheMiss,
    DailyLimitReached,
    InvalidLLMOutput,
    LLMProvider,
    TransientLLMError,
)
from adsight.llm.cache import now_iso
from adsight.llm.factory import parse_spec
from adsight.llm.types import LLMRequest, LLMResponse, request_key
from adsight.log import get_logger, set_run_id
from adsight.semantic.model import SemanticModel

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STOPPED = 3  # a clean, resumable stop: daily limit or budget
EXIT_WAITING = 4  # tick mode: waiting on an Anthropic batch (or not finished yet)
STATUS_BY_EXIT = {EXIT_STOPPED: "stopped", EXIT_WAITING: "waiting", EXIT_FAILED: "failed"}
DEFAULT_WORKERS = 8

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    questions_dir TEXT NOT NULL,
    arch TEXT NOT NULL,
    model_spec TEXT NOT NULL,
    batch INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    run_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    status TEXT NOT NULL,
    passed INTEGER,
    reason TEXT,
    answer_json TEXT,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    error TEXT,
    finished_at TEXT NOT NULL,
    PRIMARY KEY (run_id, question_id)
);
CREATE TABLE IF NOT EXISTS calls (
    run_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (run_id, question_id, key)
);
"""

# Columns added after the first release; existing databases gain them on open.
ADDED_COLUMNS = {
    "judge_passed": "INTEGER",
    "judge_json": "TEXT",
    "untraceable_numbers": "INTEGER NOT NULL DEFAULT 0",
}

log = get_logger("bench.run")


def default_run_id(arch: str, model_spec: str, questions_dir: Path) -> str:
    return f"{arch}-{parse_spec(model_spec).slug}-{questions_dir.name}"


class RunStore:
    """runs.sqlite: one row per run, one row per (run, question)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path))
        self.con.executescript(RUNS_SCHEMA)
        have = {str(r[1]) for r in self.con.execute("PRAGMA table_info(results)")}
        for column, decl in ADDED_COLUMNS.items():
            if column not in have:
                self.con.execute(f"ALTER TABLE results ADD COLUMN {column} {decl}")
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def start_run(
        self, run_id: str, questions_dir: Path, arch: str, model_spec: str, batch: bool, seed: int
    ) -> None:
        with self.con:
            self.con.execute(
                "INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
                (run_id, now_iso(), str(questions_dir), arch, model_spec, int(batch), seed),
            )
            self.con.execute("UPDATE runs SET status = 'running' WHERE run_id = ?", (run_id,))

    def set_status(self, run_id: str, status: str) -> None:
        with self.con:
            self.con.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))

    def done_ids(self, run_id: str) -> set[str]:
        rows = self.con.execute(
            "SELECT question_id FROM results WHERE run_id = ? AND status = 'done'", (run_id,)
        ).fetchall()
        return {str(r[0]) for r in rows}

    def save(self, run_id: str, question_id: str, status: str, **values: Any) -> None:
        row = {
            "passed": None,
            "reason": None,
            "answer_json": None,
            "llm_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 0.0,
            "error": None,
            "judge_passed": None,
            "judge_json": None,
            "untraceable_numbers": 0,
        } | values
        for flag in ("passed", "judge_passed"):
            if row[flag] is not None:
                row[flag] = int(row[flag])
        row |= {"run_id": run_id, "question_id": question_id, "status": status}
        row["finished_at"] = now_iso()
        names = list(row)
        with self.con:
            self.con.execute(
                f"INSERT OR REPLACE INTO results ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                [row[n] for n in names],
            )

    def save_keys(self, run_id: str, question_id: str, keys: list[str]) -> None:
        """The LLM cache keys one question's answer (and judge) used."""
        with self.con:
            self.con.executemany(
                "INSERT OR IGNORE INTO calls VALUES (?, ?, ?)",
                [(run_id, question_id, k) for k in keys],
            )

    def call_keys(self, run_id: str) -> list[str]:
        """Cache keys behind every done result of a run."""
        rows = self.con.execute(
            "SELECT DISTINCT c.key FROM calls c JOIN results r USING (run_id, question_id) "
            "WHERE c.run_id = ? AND r.status = 'done' ORDER BY c.key",
            (run_id,),
        ).fetchall()
        return [str(r[0]) for r in rows]

    def results(self, run_id: str) -> list[dict[str, Any]]:
        cursor = self.con.execute(
            "SELECT * FROM results WHERE run_id = ? ORDER BY question_id", (run_id,)
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]

    def runs(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.run_id, r.arch, r.model_spec, r.status, r.created_at, "
            "SUM(x.status = 'done') AS done, SUM(x.status = 'retry') AS retry, "
            "SUM(x.status = 'error') AS error, SUM(COALESCE(x.passed, 0)) AS passed, "
            "COALESCE(SUM(x.cost_usd), 0) AS cost_usd "
            "FROM runs r LEFT JOIN results x USING (run_id)"
        )
        params: tuple[str, ...] = ()
        if run_id is not None:
            sql += " WHERE r.run_id = ?"
            params = (run_id,)
        cursor = self.con.execute(
            sql + " GROUP BY r.run_id ORDER BY r.created_at, r.run_id", params
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


@dataclass
class RunConfig:
    questions_dir: Path
    arch: str
    model_spec: str
    batch: bool = False
    limit: int | None = None
    run_id: str | None = None
    seed: int = 7
    workers: int = DEFAULT_WORKERS
    judge: str = "off"  # a model spec, or "off"
    salt: str = ""  # run-level salt: repeat runs are independent samples
    sample: int | None = None  # N questions spread over the categories

    @property
    def resolved_run_id(self) -> str:
        return self.run_id or default_run_id(self.arch, self.model_spec, self.questions_dir)


@dataclass
class RunOutcome:
    run_id: str
    exit_code: int
    report: dict[str, Any]
    stop_reason: str | None = None
    ran: list[str] = field(default_factory=list)  # question ids attempted in this call


def load_manifest(path: Path) -> dict[str, Any] | None:
    """The data manifest (incident causes for gold root-cause references), if built."""
    if not path.is_file():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def stratified_sample(questions: list[Question], n: int, seed: int) -> list[Question]:
    """N questions spread over the categories: each category shuffled by the seed, then
    taken round-robin over the categories in file order. Returned in file order."""
    by_category: dict[str, list[Question]] = {}
    for q in questions:
        by_category.setdefault(q.category, []).append(q)
    for category, members in by_category.items():
        random.Random(f"{seed}:sample:{category}").shuffle(members)
    picked: set[str] = set()
    while len(picked) < min(n, len(questions)):
        for members in by_category.values():
            if members and len(picked) < n:
                picked.add(members.pop(0).id)
    return [q for q in questions if q.id in picked]


def load_run_questions(cfg: RunConfig) -> list[Question]:
    """Effective questions (review None or approved); --sample N spreads N over the
    categories, --limit N takes the first N."""
    questions = effective_questions(load_questions(cfg.questions_dir / "questions.jsonl"))
    if cfg.sample is not None:
        questions = stratified_sample(questions, cfg.sample, cfg.seed)
    return questions[: cfg.limit] if cfg.limit is not None else questions


def is_complete(store: RunStore, cfg: RunConfig) -> bool:
    wanted = {q.id for q in load_run_questions(cfg)}
    return wanted <= store.done_ids(cfg.resolved_run_id)


class _Recorder:
    """Passes calls through, remembering each request key (for cassettes)."""

    def __init__(self, inner: LLMProvider, keys: list[str]):
        self.inner, self.keys = inner, keys

    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        self.keys.append(request_key(req, salt))
        return await self.inner.complete(req, salt)


def build_report(
    cfg: RunConfig, store: RunStore, questions: list[Question], cache_hits: int, status: str
) -> dict[str, Any]:
    run_id = cfg.resolved_run_id
    category = {q.id: q.category for q in questions}
    rows = [r for r in store.results(run_id) if r["question_id"] in category]
    done = [r for r in rows if r["status"] == "done"]
    per_category: dict[str, dict[str, int]] = {}
    for q in questions:
        per_category.setdefault(q.category, {"passed": 0, "total": 0})
    for r in done:
        entry = per_category[category[r["question_id"]]]
        entry["total"] += 1
        entry["passed"] += int(r["passed"] or 0)
        if r.get("judge_passed") is not None:
            entry["judged"] = entry.get("judged", 0) + 1
            entry["judge_passed"] = entry.get("judge_passed", 0) + int(r["judge_passed"])
    passed = sum(int(r["passed"] or 0) for r in done)
    n = len(done)
    return {
        "run_id": run_id,
        "status": status,
        "arch": cfg.arch,
        "model_spec": cfg.model_spec,
        "questions_dir": str(cfg.questions_dir),
        "batch": cfg.batch,
        "judge": cfg.judge,
        "questions": len(questions),
        "counts": dict(Counter(r["status"] for r in rows)),
        "per_category": per_category,
        "passed": passed,
        "accuracy": round(passed / n, 4) if n else 0.0,
        "mean_llm_calls": round(sum(r["llm_calls"] for r in done) / n, 3) if n else 0.0,
        "mean_tokens": (
            round(sum(r["input_tokens"] + r["output_tokens"] for r in done) / n, 1) if n else 0.0
        ),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "cache_hits": cache_hits,
    }


async def run_benchmark(
    cfg: RunConfig,
    provider: LLMProvider,
    card: str,
    con: duckdb.DuckDBPyConnection,
    allowed_tables: set[str],
    store: RunStore,
    reports_dir: Path,
    semantic: SemanticModel | None = None,
    judge_provider: LLMProvider | None = None,
    manifest: dict[str, Any] | None = None,
    stop: dict[str, Any] | None = None,
) -> RunOutcome:
    """Run every question not yet done. `stop` may be shared by concurrent jobs: once any
    job hits a limit, a budget or a pending batch, no job starts another question."""
    run_id = cfg.resolved_run_id
    set_run_id(run_id)
    questions = load_run_questions(cfg)
    done = store.done_ids(run_id)
    todo = [q for q in questions if q.id not in done]
    store.start_run(run_id, cfg.questions_dir, cfg.arch, cfg.model_spec, cfg.batch, cfg.seed)
    fields = {"arch": cfg.arch, "model": cfg.model_spec, "todo": len(todo), "done": len(done)}
    log.info("run start", extra={"fields": fields})

    queue: asyncio.Queue[Question] = asyncio.Queue()
    for q in todo:
        queue.put_nowait(q)
    stop = {} if stop is None else stop
    ran: list[str] = []

    async def _judge(
        q: Question, answer: AgentAnswer, judge_with: LLMProvider | None
    ) -> tuple[JudgeJSON | None, dict[str, Any] | None]:
        """The judge layer for a root-cause question, or (None, None) when it does not apply."""
        if q.category != "root_cause" or judge_with is None or cfg.judge == "off":
            return None, None
        reference = reference_text(q, manifest)
        if reference is None:
            return None, {"error": "no reference text"}
        try:
            verdict = await judge_root_cause(
                judge_with, cfg.judge, q.question, answer, reference, salt=cfg.salt
            )
        except InvalidLLMOutput:
            return None, {"error": "invalid_output"}
        return verdict, verdict.model_dump()

    async def one(q: Question) -> None:
        ran.append(q.id)
        keys: list[str] = []
        judge_with = None if judge_provider is None else _Recorder(judge_provider, keys)
        try:
            result = await run_question(
                cfg.arch,
                q.question,
                _Recorder(provider, keys),
                cfg.model_spec,
                card,
                con,
                allowed_tables,
                semantic=semantic,
                salt=cfg.salt,
            )
            verdict = score(q, result.answer)
            judged = await _judge(q, result.answer, judge_with)
        except (DailyLimitReached, BudgetExceeded) as exc:
            stop.setdefault("reason", str(exc))
            stop.setdefault("exit", EXIT_STOPPED)
            store.save(run_id, q.id, "retry", error=type(exc).__name__)
            return
        except CacheMiss as exc:
            stop.setdefault("reason", f"replay cache miss on {q.id}")
            stop.setdefault("exit", EXIT_FAILED)
            store.save(run_id, q.id, "error", error=f"cache_miss: {exc.key}")
            return
        except BatchPending as exc:
            stop.setdefault("reason", str(exc))
            stop.setdefault("exit", EXIT_WAITING)
            store.save(run_id, q.id, "retry", error="waiting on a batch")
            return
        except TransientLLMError as exc:
            store.save(run_id, q.id, "retry", error=f"transient: {exc}")
            return
        judge, judge_info = judged
        passed = combine(verdict.passed, judge)
        reason = verdict.reason
        if judge is not None and not judge.passed:
            why = "invents a cause" if judge.invents_unsupported_cause else "misses the cause"
            reason = "; ".join(r for r in (reason, f"judge: {why}") if r)
        store.save(
            run_id,
            q.id,
            "done",
            passed=passed,
            reason=reason,
            judge_passed=None if judge is None else judge.passed,
            judge_json=None
            if judge_info is None
            else json.dumps({**judge_info, "rule_passed": verdict.passed}),
            untraceable_numbers=result.untraceable_numbers,
            answer_json=result.answer.model_dump_json(),
            llm_calls=result.llm_calls,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            error=result.error,
        )
        store.save_keys(run_id, q.id, keys)
        log.info(
            "question",
            extra={
                "fields": {
                    "question_id": q.id,
                    "passed": passed,
                    "llm_calls": result.llm_calls,
                    "cost_usd": result.cost_usd,
                    "error": result.error,
                }
            },
        )

    async def worker() -> None:
        while not stop and not queue.empty():
            await one(queue.get_nowait())

    workers = len(todo) if cfg.batch else min(cfg.workers, max(len(todo), 1))
    await asyncio.gather(*(worker() for _ in range(workers)))

    if stop:
        if stop["exit"] in (EXIT_STOPPED, EXIT_WAITING):
            while not queue.empty():
                store.save(run_id, queue.get_nowait().id, "retry", error="not run: stopped")
        status = STATUS_BY_EXIT[stop["exit"]]
        log.info("run stop", extra={"fields": {"reason": stop["reason"]}})
    elif is_complete(store, cfg):
        status = "complete"
    else:
        status = "incomplete"  # transient errors left "retry" rows: run again
    store.set_status(run_id, status)
    hit_sources = {id(p): p for p in (provider, judge_provider) if p is not None}
    hits = sum(int(getattr(p, "hits", 0)) for p in hit_sources.values())
    report = build_report(cfg, store, questions, hits, status)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{run_id}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    log.info("run end", extra={"fields": {"status": status, "accuracy": report["accuracy"]}})
    return RunOutcome(
        run_id=run_id,
        exit_code=stop.get("exit", EXIT_OK),
        report=report,
        stop_reason=stop.get("reason"),
        ran=ran,
    )
