"""Command-line entry point."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from adsight import __version__
from adsight.config import get_settings
from adsight.log import configure_logging, get_logger

app = typer.Typer(no_args_is_help=True, help="adsight: text-to-SQL marketing analytics agent.")
bench_app = typer.Typer(no_args_is_help=True, help="Benchmark tools.")
app.add_typer(bench_app, name="bench")
semantic_app = typer.Typer(no_args_is_help=True, help="Semantic layer tools.")
app.add_typer(semantic_app, name="semantic")
console = Console()
err_console = Console(stderr=True)


@app.callback()
def _root() -> None:
    """adsight: text-to-SQL marketing analytics agent."""


@bench_app.callback()
def _bench() -> None:
    """Benchmark tools: reference answers and scoring."""


@semantic_app.callback()
def _semantic() -> None:
    """Semantic layer tools: check a mapping against a database."""


@app.command()
def version() -> None:
    """Print the adsight version."""
    console.print(f"adsight {__version__}")


class Shape(StrEnum):
    daily = "daily"
    flat_weekly = "flat-weekly"


@app.command()
def data(
    seed: Annotated[int | None, typer.Option(help="RNG seed. Default: ADSIGHT_SEED or 7.")] = None,
    data_dir: Annotated[
        Path | None, typer.Option(help="Output folder. Default: ADSIGHT_DATA_DIR or data.")
    ] = None,
    shape: Annotated[
        Shape,
        typer.Option(help="daily: the Meta-style tables; flat-weekly: one Google-style table."),
    ] = Shape.daily,
) -> None:
    """Generate the dataset into <data-dir>/adsight.duckdb, with <data-dir>/manifest.json.

    With --shape flat-weekly, write <data-dir>/flat/adsight_flat.duckdb instead.
    """
    from adsight.data.build import build_database, build_flat_database
    from adsight.data.generator import generate
    from adsight.data.manifest import build_manifest, write_manifest

    settings = get_settings()
    if seed is not None:
        settings.seed = seed
    if data_dir is not None:
        settings.data_dir = data_dir
    configure_logging(settings.log_level)

    if shape is Shape.flat_weekly:
        flat_path = settings.data_dir / "flat" / "adsight_flat.duckdb"
        n = build_flat_database(generate(settings.seed), flat_path)
        console.print(f"adsight data (seed {settings.seed}, flat-weekly) -> {flat_path}: perf {n}")
        get_logger("data").info(
            "flat data built",
            extra={"fields": {"seed": settings.seed, "path": flat_path, "rows": n}},
        )
        return

    path = settings.db_path
    manifest_path = settings.data_dir / "manifest.json"
    dataset = generate(settings.seed)
    counts = build_database(dataset, path)
    manifest = build_manifest(dataset, settings.seed)
    write_manifest(manifest, manifest_path)

    table = Table(title=f"adsight data (seed {settings.seed}) -> {path}")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for name, count in counts.items():
        table.add_row(name, str(count))
    console.print(table)

    incidents = Table(title=f"incidents -> {manifest_path}")
    for column in ("incident_id", "kind", "scope", "metric"):
        incidents.add_column(column)
    for column in ("before", "after", "change %"):
        incidents.add_column(column, justify="right")
    for entry in manifest["incidents"]:
        measured = entry["measured"]
        incidents.add_row(
            entry["incident_id"],
            entry["kind"],
            f"{entry['scope_type']} {entry['scope_id']}",
            entry["metric"],
            _fmt(measured["before_value"]),
            _fmt(measured["after_value"]),
            _fmt(measured["change_pct"]),
        )
    console.print(incidents)

    get_logger("data").info(
        "data built",
        extra={
            "fields": {
                "seed": settings.seed,
                "path": path,
                "manifest": manifest_path,
                "counts": counts,
            }
        },
    )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


@bench_app.command()
def refs(
    questions: Annotated[Path, typer.Option(help="Questions JSONL file.")] = Path(
        "benchmark/questions.jsonl"
    ),
    sql_dir: Annotated[Path, typer.Option(help="Reference SQL folder.")] = Path(
        "benchmark/reference_sql"
    ),
    data_dir: Annotated[
        Path | None, typer.Option(help="Data folder. Default: ADSIGHT_DATA_DIR or data.")
    ] = None,
    write: Annotated[bool, typer.Option("--write", help="Store the computed answers.")] = False,
    db: Annotated[
        Path | None, typer.Option("--db", help="DuckDB file. Overrides --data-dir.")
    ] = None,
) -> None:
    """Check (or with --write, store) every question's expected answer from reference SQL."""
    import json

    from adsight.benchmark.questions import Expected, load_questions, save_questions
    from adsight.benchmark.refs import compute_expected
    from adsight.data.build import open_readonly

    settings = get_settings()
    if data_dir is not None:
        settings.data_dir = data_dir
    db_path = db if db is not None else settings.db_path
    if not db_path.is_file():
        err_console.print(f"database not found at {db_path}: run `adsight data` first")
        raise typer.Exit(2)

    loaded = load_questions(questions)
    con = open_readonly(db_path)
    try:
        computed = {
            q.id: compute_expected(q, sql_dir, con) for q in loaded if q.reference_sql is not None
        }
    finally:
        con.close()

    if write:
        updated = [
            q.model_copy(update={"expected": computed[q.id]}) if q.id in computed else q
            for q in loaded
        ]
        save_questions(updated, questions)
        console.print(f"wrote expected answers for {len(computed)} questions to {questions}")
        return

    def show(value: Expected | None) -> str:
        if value is None:
            return "missing"
        dumped = value.model_dump(mode="json", exclude_none=True)
        return json.dumps(dumped, separators=(",", ":"))

    table = Table(title=f"reference answers ({db_path})")
    for column in ("id", "stored", "computed", "status"):
        table.add_column(column)
    failures = 0
    for q in loaded:
        if q.id not in computed:
            continue
        ok = q.expected == computed[q.id]
        failures += not ok
        table.add_row(q.id, show(q.expected), show(computed[q.id]), "OK" if ok else "MISMATCH")
    console.print(table)
    if failures:
        raise typer.Exit(1)


DbOption = Annotated[
    Path | None,
    typer.Option("--db", help="DuckDB file. Default: <data dir>/adsight.duckdb."),
]


def _db_path(db: Path | None) -> Path:
    return db if db is not None else get_settings().db_path


@app.command()
def load(
    csv_files: Annotated[list[Path], typer.Argument(help="CSV files; each becomes a table.")],
    db: DbOption = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing database.")] = False,
) -> None:
    """Load CSV files into a new DuckDB database, one table per file (named after the stem)."""
    import duckdb

    path = _db_path(db)
    if path.exists() and not force:
        err_console.print(f"{path} already exists; pass --force to overwrite it")
        raise typer.Exit(1)
    for csv in csv_files:
        if not csv.is_file():
            err_console.print(f"CSV file not found: {csv}")
            raise typer.Exit(1)
    path.unlink(missing_ok=True)
    path.with_name(path.name + ".wal").unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        table = Table(title=f"loaded into {path}")
        table.add_column("table")
        table.add_column("rows", justify="right")
        for csv in csv_files:
            name = csv.stem
            source = csv.resolve().as_posix().replace("'", "''")
            con.execute(f"CREATE TABLE \"{name}\" AS SELECT * FROM read_csv_auto('{source}')")
            row = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
            table.add_row(name, str(row[0] if row else 0))
    finally:
        con.close()
    console.print(table)


@app.command()
def inspect(
    out: Annotated[Path, typer.Option("--out", help="Where to write the drafted YAML.")],
    db: DbOption = None,
    name: Annotated[str, typer.Option("--name", help="Model name.")] = "draft",
) -> None:
    """Draft a semantic model (YAML) for a database and explain each decision."""
    from adsight.data.build import open_readonly
    from adsight.semantic.inspect import InspectError, draft_model
    from adsight.semantic.model import dump_model

    path = _db_path(db)
    if not path.is_file():
        err_console.print(f"database not found at {path}")
        raise typer.Exit(1)
    con = open_readonly(path)
    try:
        model, notes = draft_model(con, name)
    except InspectError as exc:
        err_console.print(f"cannot draft a model: {exc}")
        raise typer.Exit(1) from exc
    finally:
        con.close()
    dump_model(model, out)

    roles = Table(title=f"{model.name}: role -> column")
    roles.add_column("role")
    roles.add_column("column")
    roles.add_row("fact table", model.fact.table)
    roles.add_row("date", f"{model.fact.date} (grain {model.grain})")
    for measure, column in model.fact.measures.items():
        roles.add_row(f"measure {measure}", column)
    for lv in model.levels:
        detail = f"{lv.table}.{lv.key}" + (f", label {lv.label}" if lv.label else "")
        detail += f", parent_key {lv.parent_key}" if lv.parent_key else ""
        roles.add_row(f"level {lv.name}", detail)
    for attr in model.attributes:
        roles.add_row(f"attribute {attr.name}", f"{attr.table}.{attr.column}")
    roles.add_row("change log", model.change_log.table if model.change_log else "none")
    console.print(roles)
    for note in notes:
        console.print(f"- {note}")
    console.print(f"wrote {out}")


@semantic_app.command("check")
def semantic_check(
    model_path: Annotated[Path, typer.Argument(help="Semantic model YAML.")],
    db: DbOption = None,
) -> None:
    """Check a semantic model YAML against a database."""
    from adsight.data.build import open_readonly
    from adsight.semantic.check import check_model
    from adsight.semantic.model import load_model

    path = _db_path(db)
    if not path.is_file():
        err_console.print(f"database not found at {path}")
        raise typer.Exit(1)
    model = load_model(model_path)
    con = open_readonly(path)
    try:
        problems = check_model(model, con)
    finally:
        con.close()
    if problems:
        for problem in problems:
            console.print(f"PROBLEM: {problem}")
        raise typer.Exit(1)
    console.print(f"OK: {model_path} matches {path}")


@bench_app.command()
def generate(
    semantic: Annotated[Path, typer.Option("--semantic", help="Semantic model YAML.")] = Path(
        "semantic/demo.yaml"
    ),
    db: DbOption = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output folder. Default: benchmark/generated/<name>."),
    ] = None,
    seed: Annotated[int, typer.Option("--seed", help="Selection seed.")] = 7,
    strict: Annotated[bool, typer.Option("--strict", help="Exit 1 on any shortfall.")] = False,
) -> None:
    """Generate a benchmark for any database from its semantic model."""
    from adsight.data.build import open_readonly
    from adsight.generate.generator import generate_benchmark
    from adsight.semantic.model import load_model

    path = _db_path(db)
    if not path.is_file():
        err_console.print(f"database not found at {path}")
        raise typer.Exit(1)
    model = load_model(semantic)
    out_dir = out if out is not None else Path("benchmark/generated") / model.name
    con = open_readonly(path)
    try:
        report = generate_benchmark(model, con, seed, out_dir)
    finally:
        con.close()

    table = Table(title=f"{model.name} benchmark (seed {seed}) -> {out_dir}")
    for column in ("category", "target", "selected"):
        table.add_column(column, justify="left" if column == "category" else "right")
    for category, counts in report.per_category.items():
        table.add_row(category, str(counts["target"]), str(counts["selected"]))
    console.print(table)
    console.print(f"candidates evaluated {report.candidates_evaluated}, accepted {report.accepted}")
    rules = Table(title="rejections by rule")
    rules.add_column("rule")
    rules.add_column("count", justify="right")
    for rule, count in sorted(report.rejected_by_rule.items()):
        rules.add_row(rule, str(count))
    console.print(rules)
    directions = ", ".join(f"{d} {n}" for d, n in report.trend_directions.items())
    console.print(f"trend directions: {directions}")
    if report.shortfalls:
        for shortfall in report.shortfalls:
            console.print(f"SHORTFALL: {shortfall}")
    else:
        console.print("no shortfalls")
    if strict and report.shortfalls:
        raise typer.Exit(1)


@bench_app.command()
def review(
    folder: Annotated[Path, typer.Argument(help="Benchmark folder holding questions.jsonl.")],
    approve: Annotated[
        list[str] | None, typer.Option("--approve", help="Question id to approve.")
    ] = None,
    reject: Annotated[
        list[str] | None, typer.Option("--reject", help="Question id to reject.")
    ] = None,
) -> None:
    """List questions pending human review, or approve / reject them by id."""
    from adsight.benchmark.questions import load_questions, save_questions

    path = folder / "questions.jsonl"
    questions = load_questions(path)
    decisions = {qid: "approved" for qid in approve or []} | {
        qid: "rejected" for qid in reject or []
    }
    if not decisions:
        pending = [q for q in questions if q.review == "pending"]
        table = Table(title=f"{len(pending)} questions pending review in {path}")
        for column in ("id", "question", "reference_summary"):
            table.add_column(column)
        for q in pending:
            summary = getattr(q.scoring, "reference_summary", None) or ""
            table.add_row(q.id, q.question, summary)
        console.print(table)
        return
    by_id = {q.id: q for q in questions}
    for qid in decisions:
        if qid not in by_id:
            err_console.print(f"unknown question id {qid}")
            raise typer.Exit(1)
        if by_id[qid].review != "pending":
            err_console.print(f"{qid} is not pending review (review={by_id[qid].review})")
            raise typer.Exit(1)
    updated = [
        q.model_copy(update={"review": decisions[q.id]}) if q.id in decisions else q
        for q in questions
    ]
    save_questions(updated, path)
    counts = {
        state: sum(1 for q in updated if q.review == state)
        for state in ("pending", "approved", "rejected")
    }
    console.print(", ".join(f"{state} {n}" for state, n in counts.items()))


@bench_app.command("run")
def bench_run(
    questions_dir: Annotated[
        Path, typer.Option("--questions-dir", help="Benchmark folder holding questions.jsonl.")
    ],
    arch: Annotated[str, typer.Option("--arch", help="Architecture: b0, b1, b2, b3 or b4.")],
    model: Annotated[
        str,
        typer.Option(
            "--model",
            help="<provider>:<model>, e.g. groq:openai/gpt-oss-120b, "
            "gemini:gemini-flash-lite-latest, anthropic:claude-opus-5.",
        ),
    ],
    batch: Annotated[
        bool, typer.Option("--batch", help="Anthropic only: send calls as batches.")
    ] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Run the first N only.")] = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Default: <arch>-<model>-<dir>.")
    ] = None,
    replay: Annotated[
        bool, typer.Option("--replay", help="Answer only from the LLM cache; never call out.")
    ] = False,
    semantic: Annotated[Path, typer.Option("--semantic", help="Semantic model YAML.")] = Path(
        "semantic/demo.yaml"
    ),
    db: DbOption = None,
    judge: Annotated[
        str, typer.Option("--judge", help="Model spec of the root-cause judge, or off.")
    ] = "off",
    salt: Annotated[
        str, typer.Option("--salt", help="Run salt: repeat runs become independent samples.")
    ] = "",
    sample: Annotated[
        int | None, typer.Option("--sample", help="N questions spread over the categories.")
    ] = None,
    render: Annotated[
        str,
        typer.Option(
            "--render", help="Result rendering: v4 (default) or v3 (the published v3 run)."
        ),
    ] = "v4",
    wait_minutes: Annotated[
        float | None,
        typer.Option("--wait-minutes", help="Tick mode: stop (exit 4) if a batch outlasts this."),
    ] = None,
    cassette: Annotated[
        Path | None,
        typer.Option("--cassette", help="With --replay: answer from this cassette.jsonl.gz."),
    ] = None,
) -> None:
    """Run (or resume) a benchmark: score every effective question and write a report.

    Exit codes: 3 a clean, resumable stop (daily limit or budget); 4 waiting on a batch.
    Run it again later to continue.
    """
    import asyncio

    from adsight.agent.context import model_tables, schema_card
    from adsight.architectures import ARCHITECTURES
    from adsight.bench_run import RunConfig, RunStore, load_manifest, run_benchmark
    from adsight.generate.context import build_context
    from adsight.llm.cache import ResponseCache
    from adsight.llm.factory import ProviderPool
    from adsight.semantic.model import load_model
    from adsight.sql_safety import RENDERS, open_agent_connection

    settings = get_settings()
    configure_logging(settings.log_level)
    if cassette is not None and not replay:
        err_console.print("--cassette needs --replay")
        raise typer.Exit(2)
    if render not in RENDERS:
        err_console.print(f"unknown render {render!r}; choose from {', '.join(RENDERS)}")
        raise typer.Exit(2)
    if arch not in ARCHITECTURES:
        err_console.print(f"unknown architecture {arch!r}; choose from {sorted(ARCHITECTURES)}")
        raise typer.Exit(2)
    path = _db_path(db)
    if not path.is_file():
        err_console.print(f"database not found at {path}")
        raise typer.Exit(2)
    if not (questions_dir / "questions.jsonl").is_file():
        err_console.print(f"no questions.jsonl in {questions_dir}")
        raise typer.Exit(2)
    sem = load_model(semantic)
    pool = ProviderPool(
        settings,
        batch,
        mode="replay" if replay else "normal",
        script=[] if model.startswith("fixture:") or judge.startswith("fixture:") else None,
        cache=None if cassette is None else ResponseCache.from_cassette(cassette),
        wait_s=None if wait_minutes is None else wait_minutes * 60,
    )
    provider = pool.get(model)
    judge_provider = None if judge == "off" else pool.get(judge)
    cfg = RunConfig(
        questions_dir,
        arch,
        model,
        batch,
        limit,
        run_id,
        settings.seed,
        judge=judge,
        salt=salt,
        sample=sample,
        render=render,
    )
    store = RunStore(settings.runs_db_path)
    con = open_agent_connection(path)
    try:
        card = schema_card(sem, build_context(sem, con), con)
        outcome = asyncio.run(
            run_benchmark(
                cfg,
                provider,
                card,
                con,
                set(model_tables(sem)),
                store,
                settings.reports_dir,
                semantic=sem,
                judge_provider=judge_provider,
                manifest=load_manifest(settings.data_dir / "manifest.json"),
            )
        )
    finally:
        con.close()
        store.close()
        pool.close()

    report = outcome.report
    table = Table(title=f"{outcome.run_id} ({report['status']})")
    for column in ("category", "passed", "total", "accuracy"):
        table.add_column(column, justify="left" if column == "category" else "right")
    for category, counts in report["per_category"].items():
        total = counts["total"]
        accuracy = f"{counts['passed'] / total:.0%}" if total else "-"
        table.add_row(category, str(counts["passed"]), str(total), accuracy)
    console.print(table)
    console.print(
        f"accuracy {report['accuracy']:.1%}, mean LLM calls {report['mean_llm_calls']}, "
        f"mean tokens {report['mean_tokens']}, total cost ${report['total_cost_usd']:.4f}, "
        f"cache hits {report['cache_hits']}, counts {report['counts']}"
    )
    console.print(f"judge: {report['judge']}")
    console.print(f"report: {settings.reports_dir / (outcome.run_id + '.json')}")
    if outcome.stop_reason:
        err_console.print(f"STOPPED: {outcome.stop_reason}")
    if outcome.exit_code:
        raise typer.Exit(outcome.exit_code)


@bench_app.command("resume")
def bench_resume(
    plan_path: Annotated[Path, typer.Option("--plan", help="Run plan YAML.")],
    wait_minutes: Annotated[
        float | None,
        typer.Option("--wait-minutes", help="Stop (exit 4) if a batch outlasts this."),
    ] = None,
) -> None:
    """One tick of a run plan: re-attach batches, then run every unfinished job at once.

    Exit codes: 0 every job complete; 3 budget or daily limit; 4 waiting on a batch or
    not finished yet. Run it again later to continue.
    """
    import asyncio

    from adsight.bench_run import RunStore
    from adsight.llm.factory import ProviderPool
    from adsight.plans import load_plan, plan_settings, record_progress, resume_plan

    plan = load_plan(plan_path)
    settings = plan_settings(plan, get_settings())
    configure_logging(settings.log_level)
    wait_s = None if wait_minutes is None else wait_minutes * 60
    pool = ProviderPool(settings, script=[], wait_s=wait_s)  # fixture jobs: replay only
    store = RunStore(settings.runs_db_path)
    try:
        tick = asyncio.run(resume_plan(plan, settings, pool, store))
    finally:
        store.close()
        pool.close()
    record_progress(plan, tick, settings)

    table = Table(title=f"plan {plan.name}")
    for column in ("job", "done/total", "retry", "errors", "accuracy", "cost"):
        table.add_column(column, justify="left" if column == "job" else "right")
    for p in tick.progress:
        table.add_row(
            p.job,
            f"{p.done}/{p.total}",
            str(p.retry),
            str(p.errors),
            f"{p.accuracy:.1%}",
            f"${p.cost_usd:.4f}",
        )
    console.print(table)
    console.print(
        f"spend ${tick.spent_usd:.4f} of ${tick.budget_usd:.2f} budget; "
        f"batches in flight {tick.batches_in_flight}"
    )
    if tick.reason:
        err_console.print(f"STOPPED: {tick.reason}")
    if tick.exit_code:
        raise typer.Exit(tick.exit_code)


@bench_app.command("export")
def bench_export(
    plan_path: Annotated[Path, typer.Option("--plan", help="Run plan YAML.")],
    out: Annotated[Path, typer.Option("--out", help="Output folder.")],
) -> None:
    """Write each job's report and response cassette, plus summary.json, for offline replay."""
    from adsight.bench_run import RunStore
    from adsight.llm.cache import ResponseCache
    from adsight.plans import export_plan, load_plan

    plan = load_plan(plan_path)
    settings = get_settings()
    store = RunStore(settings.runs_db_path)
    cache = ResponseCache(settings.llm_cache_path)
    try:
        summary = export_plan(plan, settings, store, cache, out)
    finally:
        store.close()
        cache.close()
    console.print(f"exported {len(summary['jobs'])} jobs to {out}")


@bench_app.command("status")
def bench_status(
    run_id: Annotated[str | None, typer.Option("--run-id", help="Show one run only.")] = None,
) -> None:
    """List benchmark runs with their done / retry / error counts and cost."""
    from adsight.bench_run import RunStore

    settings = get_settings()
    store = RunStore(settings.runs_db_path)
    try:
        runs = store.runs(run_id)
    finally:
        store.close()
    table = Table(title=f"benchmark runs ({settings.runs_db_path})")
    columns = ("run_id", "arch", "model", "status", "done", "retry", "error", "passed", "cost")
    for column in columns:
        table.add_column(column)
    for r in runs:
        table.add_row(
            r["run_id"],
            r["arch"],
            r["model_spec"],
            r["status"],
            str(r["done"] or 0),
            str(r["retry"] or 0),
            str(r["error"] or 0),
            str(r["passed"] or 0),
            f"${r['cost_usd']:.4f}",
        )
    console.print(table)


@bench_app.command("table")
def bench_table(
    results: Annotated[
        Path, typer.Option("--results", help="Exported results folder (<job>/report.json).")
    ],
    out: Annotated[Path, typer.Option("--out", help="Output folder.")] = Path("docs/results"),
) -> None:
    """Write results.md, accuracy_by_category.svg and cost_vs_accuracy.svg from the reports."""
    from adsight.results_table import write_results

    try:
        written = write_results(results, out)
    except ValueError as exc:
        err_console.print(str(exc))
        raise typer.Exit(1) from exc
    for path in written:
        console.print(f"wrote {path}")


@bench_app.command("verify-results")
def bench_verify_results(
    results: Annotated[
        Path, typer.Option("--results", help="Exported results folder (<job>/report.json).")
    ],
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="Folder with adsight.duckdb and manifest.json.")
    ] = Path("data"),
    semantic: Annotated[Path, typer.Option("--semantic", help="Semantic model YAML.")] = Path(
        "semantic/demo.yaml"
    ),
) -> None:
    """Replay every published job offline from its cassette and compare with its report.

    A job that does not match is replayed up to 3 times in all (render v3 runs can differ
    by DuckDB's order of tied rows). Exit 1 on a mismatch after 3 tries. Uses a throwaway
    runs database and never calls a provider."""
    from adsight.verify_results import verify_results

    if not (data_dir / "adsight.duckdb").is_file():
        err_console.print(f"database not found in {data_dir}; run `adsight data` first")
        raise typer.Exit(2)
    try:
        checks = verify_results(results, data_dir, semantic)
    except ValueError as exc:
        err_console.print(str(exc))
        raise typer.Exit(1) from exc
    table = Table(title=f"verify {results}")
    for column in ("job", "published", "replayed", "tries", "result"):
        table.add_column(
            column, justify="right" if column in ("published", "replayed", "tries") else "left"
        )
    for c in checks:
        replayed = "-" if c.replayed is None else f"{c.replayed:.1%}"
        table.add_row(c.job, f"{c.published:.1%}", replayed, str(c.tries), c.outcome)
    console.print(table)
    if not all(c.match for c in checks):
        raise typer.Exit(1)


@bench_app.command("judge-check")
def bench_judge_check(
    model: Annotated[str, typer.Option("--model", help="Model spec of the judge.")],
    batch: Annotated[
        bool, typer.Option("--batch", help="Anthropic only: send calls as batches.")
    ] = False,
    labels: Annotated[
        Path, typer.Option("--labels", help="Hand-labelled judge cases (JSONL).")
    ] = Path("benchmark/judge/labels.jsonl"),
) -> None:
    """Run the judge on every labelled case and report how often it agrees with the labels."""
    import asyncio
    import json

    from adsight.judge_check import judge_check, load_labels
    from adsight.llm.factory import ProviderPool, parse_spec

    settings = get_settings()
    configure_logging(settings.log_level)
    cases = load_labels(labels)
    pool = ProviderPool(settings, batch, script=[] if model.startswith("fixture:") else None)
    try:
        result = asyncio.run(judge_check(cases, pool.get(model), model))
    finally:
        pool.close()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / f"judge-check-{parse_spec(model).slug}.json"
    path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )

    n = result["cases"]
    console.print(f"judge {model}: final verdict agrees on {result['final_agreement']}/{n}")
    for name, agreed in result["field_agreement"].items():
        console.print(f"  {name}: {agreed}/{n}")
    if result["invalid"]:
        console.print(f"  invalid judge output: {result['invalid']}")
    table = Table(title="final verdicts (label vs judge)")
    for column in ("", "judge pass", "judge fail"):
        table.add_column(column, justify="left" if not column else "right")
    c = result["confusion"]
    table.add_row("label pass", str(c["both_pass"]), str(c["label_only_pass"]))
    table.add_row("label fail", str(c["judge_only_pass"]), str(c["both_fail"]))
    console.print(table)
    for d in result["disagreements"]:
        console.print(f"{d['id']}: label {d['label']} judge {d['judge']}\n  {d['reasoning']}")
    console.print(f"report: {path}")


def main() -> None:
    app()
