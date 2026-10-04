"""Draft a semantic model from an unknown database by reading its schema and data.

Every rule works on normalised column names: lower-cased, non-alphanumerics turned to "_".
The draft comes with notes explaining each decision, for a human to review.
"""

import re
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

import duckdb
from pydantic import ValidationError

from adsight.semantic.model import (
    MEASURES,
    REQUIRED_MEASURES,
    Attribute,
    ChangeLog,
    Fact,
    Level,
    MeasureName,
    SemanticModel,
)

MEASURE_SYNONYMS: dict[MeasureName, tuple[str, ...]] = {
    "spend": ("spend", "cost", "amount_spent", "spent", "spend_usd", "media_cost"),
    "impressions": ("impressions", "impr", "imps"),
    "clicks": ("clicks", "all_clicks", "clicks_all"),
    "link_clicks": ("link_clicks", "outbound_clicks"),
    "reach": ("reach", "unique_reach"),
    "conversions": ("conversions", "leads", "results", "total_conversions", "conv"),
}
DATE_NAMES = ("date", "day", "date_start", "week", "week_start", "reporting_starts")
LEVEL_SYNONYMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account", ("account", "customer", "advertiser")),
    ("campaign", ("campaign",)),
    ("ad_set", ("ad_set", "adset", "ad_group", "adgroup")),
    ("ad", ("ad", "creative", "ad_creative")),
)
ATTRIBUTE_MIN_VALUES = 2
ATTRIBUTE_MAX_VALUES = 20


class InspectError(Exception):
    """The database cannot be mapped: a required piece is missing."""


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", name.lower())


@dataclass
class _Column:
    name: str
    norm: str
    data_type: str

    @property
    def is_date(self) -> bool:
        return self.data_type.upper() == "DATE" or self.data_type.upper().startswith("TIMESTAMP")

    @property
    def is_text(self) -> bool:
        return self.data_type.upper() == "VARCHAR"


@dataclass
class _Table:
    name: str
    columns: list[_Column] = field(default_factory=list)
    rows: int = 0

    def find(self, norm: str) -> _Column | None:
        return next((c for c in self.columns if c.norm == norm), None)

    def date_column(self) -> _Column | None:
        return next((c for c in self.columns if c.is_date and c.norm in DATE_NAMES), None)


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _read_schema(con: duckdb.DuckDBPyConnection) -> list[_Table]:
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
    ).fetchall()
    tables: dict[str, _Table] = {}
    for table, column, data_type in rows:
        tables.setdefault(table, _Table(table)).columns.append(
            _Column(column, normalise(column), str(data_type))
        )
    for t in tables.values():
        t.rows = _scalar(con, f"SELECT COUNT(*) FROM {_q(t.name)}")
    return list(tables.values())


def _measures(table: _Table) -> dict[MeasureName, str]:
    found: dict[MeasureName, str] = {}
    for measure in MEASURES:
        for synonym in MEASURE_SYNONYMS[measure]:
            col = table.find(synonym)
            if col is not None:
                found[measure] = col.name
                break
    return found


def _grain(con: duckdb.DuckDBPyConnection, table: str, column: str) -> Literal["day", "week"]:
    days = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT CAST({_q(column)} AS DATE) FROM {_q(table)} "
            f"WHERE {_q(column)} IS NOT NULL ORDER BY 1"
        ).fetchall()
    ]
    gaps = {(b - a).days for a, b in pairwise(days)}
    return "week" if gaps == {7} else "day"


def _is_unique(con: duckdb.DuckDBPyConnection, table: _Table, column: str) -> bool:
    if table.rows == 0:
        return False
    distinct = _scalar(con, f"SELECT COUNT(DISTINCT {_q(column)}) FROM {_q(table.name)}")
    return distinct == table.rows


def _distinct_count(con: duckdb.DuckDBPyConnection, table: str, column: str) -> int:
    return _scalar(con, f"SELECT COUNT(DISTINCT {_q(column)}) FROM {_q(table)}")


def _own_table(
    con: duckdb.DuckDBPyConnection, holders: list[_Table], fact: _Table, key_norm: str
) -> _Table | None:
    """The table a level lives in: its key is the first column there, or unique there."""
    others = [t for t in holders if t is not fact]
    for t in others:
        if t.columns[0].norm == key_norm:
            return t
    for t in others:
        col = t.find(key_norm)
        if col is not None and _is_unique(con, t, col.name):
            return t
    return None


@dataclass
class _Draft:
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)


def _find_levels(
    con: duckdb.DuckDBPyConnection, tables: list[_Table], fact: _Table, draft: _Draft
) -> list[Level]:
    levels: list[Level] = []
    for level_name, synonyms in LEVEL_SYNONYMS:
        match: tuple[str, str, list[_Table]] | None = None  # synonym, key norm, tables
        for synonym in synonyms:
            for key_norm in (f"{synonym}_id", synonym):
                holders = [t for t in tables if t.find(key_norm) is not None]
                if key_norm == synonym:
                    holders = [t for t in holders if (c := t.find(key_norm)) and c.is_text]
                if holders:
                    match = (synonym, key_norm, holders)
                    break
            if match:
                break
        if match is None:
            draft.note(f"level {level_name}: no column named like {synonyms[0]}_id; skipped")
            continue
        synonym, key_norm, holders = match
        own = _own_table(con, holders, fact, key_norm)
        table = own or fact
        key_col = table.find(key_norm) or holders[0].find(key_norm)
        if key_col is None:  # pragma: no cover - holders all contain key_norm
            raise InspectError(f"level {level_name}: key column {key_norm} vanished")
        label_col = table.find(f"{synonym}_name") or table.find("name")
        parent_key: str | None = None
        if levels and table is not fact:
            parent = levels[-1]
            parent_col = table.find(normalise(parent.key))
            parent_key = parent_col.name if parent_col else None
        where = f"its own table {table.name}" if table is not fact else "the fact table"
        draft.note(
            f"level {level_name}: key {key_col.name} in {where}"
            + (f", label {label_col.name}" if label_col else ", no label column")
            + (f", parent_key {parent_key}" if parent_key else "")
        )
        levels.append(
            Level(
                name=level_name,
                table=table.name,
                key=key_col.name,
                label=label_col.name if label_col else None,
                parent_key=parent_key,
            )
        )
    return levels


def _find_attributes(
    con: duckdb.DuckDBPyConnection, tables: list[_Table], levels: list[Level], draft: _Draft
) -> list[Attribute]:
    by_name = {t.name: t for t in tables}
    reserved = {
        (lv.table, col)
        for lv in levels
        for col in (lv.key, lv.label, lv.parent_key)
        if col is not None
    }
    # every level key is a key wherever it appears, not only in its own table
    key_norms = {normalise(lv.key) for lv in levels}
    attributes: list[Attribute] = []
    seen_tables: list[str] = []
    for lv in levels:
        if lv.table in seen_tables:
            continue
        seen_tables.append(lv.table)
        for col in by_name[lv.table].columns:
            if not col.is_text or (lv.table, col.name) in reserved or col.norm in key_norms:
                continue
            n = _distinct_count(con, lv.table, col.name)
            if not ATTRIBUTE_MIN_VALUES <= n <= ATTRIBUTE_MAX_VALUES:
                draft.note(f"attribute skipped: {lv.table}.{col.name} has {n} distinct values")
                continue
            name = col.name
            if any(a.name == name for a in attributes):
                name = f"{lv.name}_{col.name}"
            attributes.append(Attribute(name=name, table=lv.table, column=col.name))
            draft.note(f"attribute {name}: {lv.table}.{col.name} ({n} values)")
    return attributes


def _find_change_log(tables: list[_Table], fact: _Table, draft: _Draft) -> ChangeLog | None:
    for t in tables:
        if t is fact:
            continue
        date_col = t.date_column()
        change = t.find("change_type") or t.find("change")
        ids = [c for c in t.columns if c.norm.endswith("entity_id")] or [
            c for c in t.columns if c.norm.endswith("_id")
        ]
        if date_col and change and ids:
            entity_type = t.find("entity_type")
            old, new = t.find("old_value"), t.find("new_value")
            draft.note(
                f"change log: {t.name} (date {date_col.name}, entity {ids[0].name}, "
                f"change {change.name})"
            )
            return ChangeLog(
                table=t.name,
                date=date_col.name,
                entity_type=entity_type.name if entity_type else None,
                entity_id=ids[0].name,
                change_type=change.name,
                old_value=old.name if old else None,
                new_value=new.name if new else None,
            )
    draft.note("change log: none found")
    return None


def draft_model(con: duckdb.DuckDBPyConnection, name: str) -> tuple[SemanticModel, list[str]]:
    """Propose a semantic model for the database behind `con`, with notes on every decision."""
    draft = _Draft()
    tables = _read_schema(con)
    candidates = [(t, _measures(t)) for t in tables if t.date_column() is not None]
    if not candidates:
        raise InspectError(f"no table has a date column named one of {', '.join(DATE_NAMES)}")
    fact, measures = max(candidates, key=lambda tm: (len(tm[1]), tm[0].rows))
    missing = [m for m in REQUIRED_MEASURES if m not in measures]
    if missing:
        raise InspectError(f"required measures not found: {', '.join(missing)}")
    date_column = fact.date_column()
    if date_column is None:  # pragma: no cover - candidates all have a date column
        raise InspectError(f"fact table {fact.name} lost its date column")
    draft.note(f"fact table: {fact.name} ({fact.rows} rows, {len(measures)} measures matched)")
    for measure in MEASURES:
        draft.note(
            f"measure {measure}: {measures[measure]}"
            if measure in measures
            else f"measure {measure}: not found"
        )
    grain = _grain(con, fact.name, date_column.name)
    draft.note(f"date: {date_column.name}, grain {grain}")

    levels = _find_levels(con, tables, fact, draft)
    if not levels:
        raise InspectError("no entity levels found (account, campaign, ad set or ad ids)")
    attributes = _find_attributes(con, tables, levels, draft)
    change_log = _find_change_log(tables, fact, draft)
    conversion_name = "lead" if normalise(measures["conversions"]) == "leads" else "conversion"
    draft.note(f"conversion_name: {conversion_name}")

    try:
        model = SemanticModel(
            version=1,
            name=name,
            conversion_name=conversion_name,
            grain=grain,
            fact=Fact(table=fact.name, date=date_column.name, measures=measures),
            levels=levels,
            attributes=attributes,
            change_log=change_log,
        )
    except ValidationError as exc:
        raise InspectError(f"the draft is not a valid model: {exc}") from exc
    return model, draft.notes
