"""Command-line entry point."""

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


@app.command()
def data(
    seed: Annotated[int | None, typer.Option(help="RNG seed. Default: ADSIGHT_SEED or 7.")] = None,
    data_dir: Annotated[
        Path | None, typer.Option(help="Output folder. Default: ADSIGHT_DATA_DIR or data.")
    ] = None,
) -> None:
    """Generate the dataset into <data-dir>/adsight.duckdb, with <data-dir>/manifest.json."""
    from adsight.data.build import build_database
    from adsight.data.generator import generate
    from adsight.data.manifest import build_manifest, write_manifest

    settings = get_settings()
    if seed is not None:
        settings.seed = seed
    if data_dir is not None:
        settings.data_dir = data_dir
    configure_logging(settings.log_level)

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
) -> None:
    """Check (or with --write, store) every question's expected answer from reference SQL."""
    import json

    from adsight.benchmark.questions import Expected, load_questions, save_questions
    from adsight.benchmark.refs import compute_expected
    from adsight.data.build import open_readonly

    settings = get_settings()
    if data_dir is not None:
        settings.data_dir = data_dir
    db_path = settings.db_path
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


def main() -> None:
    app()
