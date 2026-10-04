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
console = Console()
err_console = Console(stderr=True)


@app.callback()
def _root() -> None:
    """adsight: text-to-SQL marketing analytics agent."""


@bench_app.callback()
def _bench() -> None:
    """Benchmark tools: reference answers and scoring."""


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


def main() -> None:
    app()
