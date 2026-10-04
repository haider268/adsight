"""Write a generated Dataset into a fresh DuckDB file."""

import csv
from collections.abc import Iterable, Sequence
from importlib.resources import files
from pathlib import Path

import duckdb

from adsight.data.generator import Dataset

TABLES = ("accounts", "campaigns", "ad_sets", "ads", "daily_ad_metrics", "events_log")

Cell = str | int | float | None


def schema_sql() -> str:
    return (files("adsight.data") / "schema.sql").read_text(encoding="utf-8")


def _copy_in(
    con: duckdb.DuckDBPyConnection, table: str, rows: Iterable[Sequence[Cell]], scratch: Path
) -> None:
    """Load rows through a temporary CSV and COPY.

    Binding Python values one by one is very slow here: without numpy or pandas installed,
    duckdb retries those optional imports per value (about 40 s for the metrics table).
    """
    with scratch.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(["" if v is None else v for v in row] for row in rows)
    try:
        source = scratch.as_posix().replace("'", "''")
        con.execute(f"COPY {table} FROM '{source}' (FORMAT csv, HEADER false)")
    finally:
        scratch.unlink(missing_ok=True)


def build_database(dataset: Dataset, path: Path) -> dict[str, int]:
    """Recreate the database at `path` from scratch and return {table: row_count}."""
    wal = path.with_name(path.name + ".wal")
    for stale in (path, wal):
        stale.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.load.csv")

    rows: dict[str, list[Sequence[Cell]]] = {
        "accounts": [(a.account_id, a.name, a.vertical) for a in dataset.accounts],
        "campaigns": [
            (c.campaign_id, c.account_id, c.name, c.objective, c.status, c.start_date.isoformat())
            for c in dataset.campaigns
        ],
        "ad_sets": [
            (s.ad_set_id, s.campaign_id, s.name, s.audience, s.placement, f"{s.daily_budget:.2f}")
            for s in dataset.ad_sets
        ],
        "ads": [
            (
                a.ad_id,
                a.ad_set_id,
                a.name,
                a.creative_type,
                a.headline,
                a.launched_date.isoformat(),
                a.status,
            )
            for a in dataset.ads
        ],
        "daily_ad_metrics": [
            (
                m.date.isoformat(),
                m.ad_id,
                m.impressions,
                m.reach,
                m.clicks,
                m.link_clicks,
                f"{m.spend:.2f}",
                m.leads,
                repr(m.frequency),
            )
            for m in dataset.metrics
        ],
        "events_log": [
            (
                event_id,
                e.date.isoformat(),
                e.entity_type,
                e.entity_id,
                e.change_type,
                e.old_value,
                e.new_value,
            )
            for event_id, e in enumerate(dataset.events, start=1)
        ],
    }

    con = duckdb.connect(str(path))
    try:
        con.execute(schema_sql())
        for table in TABLES:
            _copy_in(con, table, rows[table], scratch)
        counts: dict[str, int] = {}
        for table in TABLES:
            row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(row[0]) if row else 0
    finally:
        con.close()
    return counts


def open_readonly(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path), read_only=True)
