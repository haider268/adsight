"""`adsight bench verify-results`: replay every published job offline and compare.

Each job folder holds report.json and cassette.jsonl.gz. The job is re-run in replay mode
from its cassette alone, with the render version its report records, against a throwaway
runs database, and its accuracy and per-category passed counts must equal the published
report. Nothing can call out: the Anthropic client is replaced by one that refuses every
request, so no key is read or used.

Runs recorded with render "v3" can differ by chance: DuckDB does not always return rows
that tie on every ORDER BY key in the same order. A job that does not match is therefore
replayed up to MAX_TRIES times in all. Render "v4" runs break those ties and match first
time.
"""

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adsight.agent.context import model_tables, schema_card
from adsight.bench_run import RunConfig, RunStore, load_manifest, report_render, run_benchmark
from adsight.config import Settings
from adsight.generate.context import build_context
from adsight.llm.cache import ResponseCache
from adsight.llm.factory import ProviderPool
from adsight.semantic.model import load_model
from adsight.sql_safety import open_agent_connection

MAX_TRIES = 3


class OfflineOnly(RuntimeError):
    """A replay tried to reach a provider."""


class NoNetworkClient:
    """Stands in for the Anthropic SDK client during a replay: any use is an error."""

    def __getattr__(self, name: str) -> Any:
        raise OfflineOnly(f"replay tried to use the provider client ({name})")


@dataclass(frozen=True)
class JobCheck:
    job: str
    published: float
    replayed: float | None
    match: bool
    tries: int = 1
    detail: str = ""

    @property
    def outcome(self) -> str:
        if self.match:
            return "match" if self.tries == 1 else f"match on retry {self.tries - 1}"
        return f"mismatch after {self.tries} tries ({self.detail})"


def _passed_by_category(report: dict[str, Any]) -> dict[str, int]:
    return {c: int(v["passed"]) for c, v in report["per_category"].items()}


def replay_job(job_dir: Path, data_dir: Path, semantic: Path) -> tuple[dict[str, Any], int]:
    """Re-run one published job from its cassette; return (replayed report, exit code)."""
    published = json.loads((job_dir / "report.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="adsight-verify-") as tmp:
        settings = Settings(_env_file=None, data_dir=data_dir, state_dir=Path(tmp))  # type: ignore[call-arg]
        cfg = RunConfig(
            Path(published["questions_dir"]),
            str(published["arch"]),
            str(published["model_spec"]),
            batch=bool(published.get("batch", False)),
            run_id=str(published["run_id"]),
            seed=settings.seed,
            judge=str(published.get("judge", "off")),
            salt=str(published.get("salt") or ""),
            sample=published.get("sample"),
            render=report_render(published),
        )
        pool = ProviderPool(
            settings,
            cfg.batch,
            mode="replay",
            client=NoNetworkClient(),
            cache=ResponseCache.from_cassette(job_dir / "cassette.jsonl.gz"),
        )
        store = RunStore(settings.runs_db_path)
        model = load_model(semantic)
        con = open_agent_connection(settings.db_path)
        try:
            card = schema_card(model, build_context(model, con), con)
            outcome = asyncio.run(
                run_benchmark(
                    cfg,
                    pool.get(cfg.model_spec),
                    card,
                    con,
                    set(model_tables(model)),
                    store,
                    settings.reports_dir,
                    semantic=model,
                    judge_provider=None if cfg.judge == "off" else pool.get(cfg.judge),
                    manifest=load_manifest(data_dir / "manifest.json"),
                )
            )
        finally:
            con.close()
            store.close()
            pool.close()
    return outcome.report, outcome.exit_code


def verify_results(results: Path, data_dir: Path, semantic: Path) -> list[JobCheck]:
    jobs = sorted(p.parent for p in results.glob("*/report.json"))
    if not jobs:
        raise ValueError(f"no <job>/report.json under {results}")
    return [check_job(job_dir, data_dir, semantic) for job_dir in jobs]


def check_job(
    job_dir: Path, data_dir: Path, semantic: Path, max_tries: int = MAX_TRIES
) -> JobCheck:
    """Replay one job until it matches, at most `max_tries` times."""
    published = json.loads((job_dir / "report.json").read_text(encoding="utf-8"))
    replayed_accuracy: float | None = None
    problems: list[str] = []
    for tries in range(1, max_tries + 1):
        try:
            replayed, code = replay_job(job_dir, data_dir, semantic)
        except Exception as exc:  # a broken job is a mismatch, never a crash
            problems = [type(exc).__name__]
            continue
        replayed_accuracy = replayed["accuracy"]
        problems = []
        if code != 0:
            problems.append(f"replay exit {code}")
        if replayed["accuracy"] != published["accuracy"]:
            problems.append("accuracy differs")
        if _passed_by_category(replayed) != _passed_by_category(published):
            problems.append("category counts differ")
        if not problems:
            return JobCheck(job_dir.name, published["accuracy"], replayed_accuracy, True, tries)
    return JobCheck(
        job_dir.name,
        published["accuracy"],
        replayed_accuracy,
        False,
        max_tries,
        "; ".join(problems),
    )
