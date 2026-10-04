"""Check a semantic model against a real database. An empty problem list means it fits."""

from itertools import pairwise

import duckdb

from adsight.semantic.model import SemanticModel

_NUMERIC_PREFIXES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
)


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _columns(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, str]]:
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    ).fetchall()
    tables: dict[str, dict[str, str]] = {}
    for table, column, data_type in rows:
        tables.setdefault(table, {})[column] = str(data_type).upper()
    return tables


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def check_model(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> list[str]:
    """Problems found when matching `model` to the database; [] means OK."""
    schema = _columns(con)
    problems: list[str] = []

    def has(table: str, column: str, role: str) -> bool:
        if table not in schema:
            problems.append(f"{role}: table {table!r} does not exist")
            return False
        if column not in schema[table]:
            problems.append(f"{role}: column {table}.{column} does not exist")
            return False
        return True

    fact = model.fact
    if has(fact.table, fact.date, "fact date"):
        dtype = schema[fact.table][fact.date]
        if not (dtype == "DATE" or dtype.startswith("TIMESTAMP")):
            problems.append(f"fact date: {fact.table}.{fact.date} is {dtype}, not a date")
    for measure, column in fact.measures.items():
        if has(fact.table, column, f"measure {measure}"):
            dtype = schema[fact.table][column]
            if not dtype.startswith(_NUMERIC_PREFIXES):
                problems.append(f"measure {measure}: {fact.table}.{column} is {dtype}, not numeric")

    for lv in model.levels:
        has(lv.table, lv.key, f"level {lv.name} key")
        if lv.label is not None:
            has(lv.table, lv.label, f"level {lv.name} label")
        if lv.parent_key is not None:
            has(lv.table, lv.parent_key, f"level {lv.name} parent_key")
    for attr in model.attributes:
        has(attr.table, attr.column, f"attribute {attr.name}")
    if model.change_log is not None:
        log = model.change_log
        log_columns: tuple[tuple[str, str | None], ...] = (
            ("date", log.date),
            ("entity_type", log.entity_type),
            ("entity_id", log.entity_id),
            ("change_type", log.change_type),
            ("old_value", log.old_value),
            ("new_value", log.new_value),
        )
        for role, log_column in log_columns:
            if log_column is not None:
                has(log.table, log_column, f"change_log {role}")
    if problems:
        return problems  # the data checks below need the columns to exist

    bottom = model.levels[-1]
    if bottom.key not in schema[fact.table]:
        problems.append(
            f"level {bottom.name}: lowest-level key {bottom.key} is not a column of {fact.table}"
        )
    elif not model.in_fact(bottom):
        orphans = _count(
            con,
            f"SELECT COUNT(*) FROM {_q(fact.table)} f WHERE f.{_q(bottom.key)} IS NOT NULL "
            f"AND f.{_q(bottom.key)} NOT IN "
            f"(SELECT {_q(bottom.key)} FROM {_q(bottom.table)})",
        )
        if orphans:
            problems.append(
                f"level {bottom.name}: {orphans} fact rows have a {bottom.key} "
                f"missing from {bottom.table}"
            )

    for parent, child in pairwise(model.levels):
        if child.parent_key is None:
            continue
        orphans = _count(
            con,
            f"SELECT COUNT(*) FROM {_q(child.table)} c WHERE c.{_q(child.parent_key)} IS NOT NULL "
            f"AND c.{_q(child.parent_key)} NOT IN "
            f"(SELECT {_q(parent.key)} FROM {_q(parent.table)})",
        )
        if orphans:
            problems.append(
                f"level {child.name}: {orphans} rows have a {child.parent_key} "
                f"missing from {parent.table}.{parent.key}"
            )

    days = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT CAST({_q(fact.date)} AS DATE) FROM {_q(fact.table)} "
            f"WHERE {_q(fact.date)} IS NOT NULL ORDER BY 1"
        ).fetchall()
    ]
    step = 7 if model.grain == "week" else 1
    gaps = sorted({(b - a).days for a, b in pairwise(days)} - {step})
    if gaps:
        problems.append(
            f"grain {model.grain}: dates are expected {step} day(s) apart, "
            f"but some are {', '.join(str(g) for g in gaps)} apart"
        )
    return problems
