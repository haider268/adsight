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
console = Console()


@app.callback()
def _root() -> None:
    """adsight: text-to-SQL marketing analytics agent."""


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


def main() -> None:
    app()
