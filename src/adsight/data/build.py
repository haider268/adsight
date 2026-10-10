"""Write a generated Dataset into a fresh DuckDB file."""

import csv
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

import duckdb

from adsight.data.catalog import WINDOW_END, WINDOW_START
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


FLAT_SCHEMA = (
    "CREATE TABLE perf (week_start DATE, campaign_id VARCHAR, campaign_name VARCHAR, "
    "ad_group VARCHAR, cost DOUBLE, impr BIGINT, clicks BIGINT, conversions BIGINT)"
)


def full_weeks() -> list[date]:
    """Monday starts of the weeks that lie entirely inside the data window."""
    first = WINDOW_START + timedelta(days=-WINDOW_START.weekday() % 7)
    weeks: list[date] = []
    while first + timedelta(days=6) <= WINDOW_END:
        weeks.append(first)
        first += timedelta(days=7)
    return weeks


def flat_rows(dataset: Dataset) -> list[tuple[Cell, ...]]:
    """The dataset as one Google-style weekly table: campaign x ad group x full week.

    Spend is summed from the cent-rounded daily values, so totals match the daily table.
    """
    weeks = set(full_weeks())
    ad_set_of = {a.ad_id: a.ad_set_id for a in dataset.ads}
    sets = {s.ad_set_id: s for s in dataset.ad_sets}
    campaigns = {c.campaign_id: c for c in dataset.campaigns}
    totals: dict[tuple[date, str], list[Decimal | int]] = {}
    for m in dataset.metrics:
        week = m.date - timedelta(days=m.date.weekday())
        if week not in weeks:
            continue
        acc = totals.setdefault((week, ad_set_of[m.ad_id]), [Decimal(0), 0, 0, 0])
        acc[0] += Decimal(f"{m.spend:.2f}")
        acc[1] += m.impressions
        acc[2] += m.clicks
        acc[3] += m.leads
    rows: list[tuple[Cell, ...]] = []
    for (week, ad_set_id), (cost, impr, clicks, conversions) in totals.items():
        campaign = campaigns[sets[ad_set_id].campaign_id]
        rows.append(
            (
                week.isoformat(),
                campaign.campaign_id,
                campaign.name,
                sets[ad_set_id].name,
                str(cost),
                int(impr),
                int(clicks),
                int(conversions),
            )
        )
    rows.sort(key=lambda r: (str(r[0]), str(r[1]), str(r[3])))
    return rows


def build_flat_database(dataset: Dataset, path: Path) -> int:
    """Recreate a one-table weekly database (no accounts, reach or change log) at `path`
    and return its row count."""
    wal = path.with_name(path.name + ".wal")
    for stale in (path, wal):
        stale.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(FLAT_SCHEMA)
        _copy_in(con, "perf", flat_rows(dataset), path.with_name(f".{path.name}.load.csv"))
        row = con.execute("SELECT COUNT(*) FROM perf").fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def open_readonly(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path), read_only=True)
