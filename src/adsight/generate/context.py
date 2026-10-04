"""The dataset context a generated benchmark is written against: dates and wording."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb

from adsight.semantic.model import SemanticModel


@dataclass(frozen=True)
class DatasetContext:
    name: str
    grain: str
    data_start: date
    data_end: date
    today: date
    conversion_name: str
    conversion_plural: str


def build_context(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> DatasetContext:
    """Dates come from the fact table; a weekly dataset ends 6 days after its last week start."""
    column = '"' + model.fact.date.replace('"', '""') + '"'
    table = '"' + model.fact.table.replace('"', '""') + '"'
    row = con.execute(
        f"SELECT MIN(CAST({column} AS DATE)), MAX(CAST({column} AS DATE)) FROM {table}"
    ).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"fact table {model.fact.table} has no dates")
    start, end = row[0], row[1]
    if model.grain == "week":
        end = end + timedelta(days=6)
    return DatasetContext(
        name=model.name,
        grain=model.grain,
        data_start=start,
        data_end=end,
        today=end + timedelta(days=1),
        conversion_name=model.conversion_name,
        conversion_plural=model.conversion_name + "s",
    )


def write_context(ctx: DatasetContext, path: Path) -> None:
    data = {
        "name": ctx.name,
        "grain": ctx.grain,
        "data_start": ctx.data_start.isoformat(),
        "data_end": ctx.data_end.isoformat(),
        "today": ctx.today.isoformat(),
        "conversion_name": ctx.conversion_name,
        "conversion_plural": ctx.conversion_plural,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
