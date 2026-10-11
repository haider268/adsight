"""The schema card: the compact, deterministic description of a dataset the LLM sees.

It is resent on every call, so it is terse (one line per item) and byte-identical for
every question, which lets providers cache it as a shared prompt prefix.
"""

import duckdb

from adsight.generate.context import DatasetContext
from adsight.semantic.metrics import available_metrics, sql_expr
from adsight.semantic.model import Level, SemanticModel
from adsight.semantic.sql import from_clause
from adsight.sql_safety import query

MAX_ATTRIBUTE_VALUES = 12
MAX_LISTED_ENTITIES = 100  # list every name up to this many
LONG_LIST_SHOWN = 20  # above it, show this many and say how to check the rest

RULES = (
    "Use only the tables and columns listed. Write DuckDB SQL. Read-only: SELECT only.\n"
    "Ratios are computed from sums (SUM(a)/SUM(b)), never averages of daily ratios.\n"
    "'Last 30 days' means the 30 days ending on the last data date.\n"
    "If the question is ambiguous, ask a clarifying question or state your assumption.\n"
    "If the data cannot answer it, say so and why; never invent numbers.\n"
    "Every number you report must come from a query result.\n"
    "Names in double quotes in a question are exact names from the data: filter on the "
    "label column with that exact string.\n"
    "Ask a clarifying question only when the answer depends on a choice you would have to "
    "guess, such as which account, metric or period; otherwise answer.\n"
    "A 'why' question asks for the most likely cause the data supports. Compare the metric "
    "in the period with the period before; check the change log for changes to the entity, "
    "its parents and its children; and check the inputs (spend, impressions, clicks, "
    "conversions per click). Name the cause the evidence supports, or say plainly that no "
    "logged change explains it and what the data shows instead. Do not refuse a why question "
    "because the data has no labelled causes."
)
CHANGE_VALUES_NOTE = (
    "old_value and new_value hold the value before and after the change, as text (for "
    "example budgets, audiences or statuses)."
)


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def model_tables(model: SemanticModel) -> list[str]:
    """Every table the model maps, fact first, in a stable order."""
    tables = [model.fact.table]
    for name in [lv.table for lv in model.levels] + [a.table for a in model.attributes]:
        if name not in tables:
            tables.append(name)
    if model.change_log is not None and model.change_log.table not in tables:
        tables.append(model.change_log.table)
    return tables


def _mapped_columns(model: SemanticModel) -> dict[str, dict[str, str]]:
    """table -> column -> what it is, in mapping order."""
    cols: dict[str, dict[str, str]] = {t: {} for t in model_tables(model)}
    fact = cols[model.fact.table]
    fact[model.fact.date] = f"date ({model.grain})"
    for measure, column in model.fact.measures.items():
        fact[column] = f"measure {measure}"
    for lv in model.levels:
        cols[lv.table].setdefault(lv.key, f"{lv.name} key")
        if lv.label:
            cols[lv.table].setdefault(lv.label, f"{lv.name} name")
        if lv.parent_key:
            cols[lv.table].setdefault(lv.parent_key, "parent key")
    for attr in model.attributes:
        cols[attr.table].setdefault(attr.column, f"attribute {attr.name}")
    if model.change_log is not None:
        log = model.change_log
        for role in ("date", "entity_type", "entity_id", "change_type", "old_value", "new_value"):
            column = getattr(log, role)
            if column:
                cols[log.table].setdefault(column, role)
    return cols


def _types(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    rows = query(con, f"DESCRIBE {_q(table)}")
    return {str(r[0]): str(r[1]) for r in rows}


def _values(con: duckdb.DuckDBPyConnection, table: str, column: str) -> str:
    rows = query(
        con,
        f"SELECT DISTINCT CAST({_q(column)} AS VARCHAR) AS v FROM {_q(table)} "
        f"WHERE {_q(column)} IS NOT NULL ORDER BY v LIMIT {MAX_ATTRIBUTE_VALUES + 1}",
    )
    values = [str(r[0]) for r in rows]
    more = len(values) > MAX_ATTRIBUTE_VALUES
    return " | ".join(values[:MAX_ATTRIBUTE_VALUES]) + (" | ..." if more else "")


def _change_log_detail(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> str:
    """What the change log records: its entity types and change types (distinct, sorted)."""
    log = model.change_log
    assert log is not None
    parts = []
    for role, column in (("entity types", log.entity_type), ("change types", log.change_type)):
        if column:
            rows = query(
                con,
                f"SELECT DISTINCT CAST({_q(column)} AS VARCHAR) AS v FROM {_q(log.table)} "
                f"WHERE {_q(column)} IS NOT NULL ORDER BY v",
            )
            parts.append(f"{role}: " + ", ".join(str(r[0]) for r in rows))
    if log.old_value or log.new_value:
        parts.append(CHANGE_VALUES_NOTE)
    return "".join(f"; {p}" for p in parts)


def entity_line(model: SemanticModel, level: Level, con: duckdb.DuckDBPyConnection) -> str:
    """`ad (54): "label", ...`: the level's entity count and EVERY name in key order
    (labels, or keys when the level has no label) when there are at most 100; otherwise
    the first 20 and a note saying how many more there are and where to look."""
    key = _q(level.key)
    shown = _q(level.label) if level.label else key
    rows = query(
        con,
        f"SELECT {key}, MIN(CAST({shown} AS VARCHAR)) FROM {_q(level.table)} "
        f"WHERE {key} IS NOT NULL GROUP BY {key} ORDER BY {key}",
    )
    count = len(rows)
    if count <= MAX_LISTED_ENTITIES:
        return f"{level.name} ({count}): " + ", ".join(f'"{r[1]}"' for r in rows)
    first = ", ".join(f'"{r[1]}"' for r in rows[:LONG_LIST_SHOWN])
    return (
        f"{level.name} ({count}): {first} ... and {count - LONG_LIST_SHOWN} more; "
        f"query the {level.table} table to check a name."
    )


def schema_card(model: SemanticModel, ctx: DatasetContext, con: duckdb.DuckDBPyConnection) -> str:
    grain = "daily" if model.grain == "day" else "weekly (dates are Monday week starts)"
    bottom = model.levels[-1].name
    lines = [
        f"DATA {model.name}: {grain} rows, {ctx.data_start} to {ctx.data_end}; "
        f"today is {ctx.today}. Money in {model.currency}.",
        f"Conversion: {model.conversion_name} "
        f"(measure conversions = {model.fact.table}.{model.fact.measures['conversions']}).",
        "",
        "TABLES (column TYPE: role)",
    ]
    for table, columns in _mapped_columns(model).items():
        types = _types(con, table)
        role = ""
        if table == model.fact.table:
            role = f" [fact: one row per {bottom} per {model.grain}]"
        elif model.change_log is not None and table == model.change_log.table:
            role = " [change log]"
        items = ", ".join(f"{c} {types.get(c, '?')}: {what}" for c, what in columns.items())
        others = [f"{c} {t}" for c, t in types.items() if c not in columns]
        if others:  # unmapped columns, so the agent can still use them (e.g. budgets)
            items += "; other columns: " + ", ".join(others)
        if model.change_log is not None and table == model.change_log.table:
            items += _change_log_detail(model, con)
        lines.append(f"{table}{role}: {items}")

    lines += ["", "HIERARCHY: " + " > ".join(lv.name for lv in model.levels)]
    lines.append("ENTITIES (count: names in key order)")
    lines += [entity_line(model, lv, con) for lv in model.levels]
    lines.append("JOIN (all levels):")
    lines += from_clause(model, [lv.name for lv in model.levels], []).splitlines()

    if model.attributes:
        lines += ["", "ATTRIBUTES (values)"]
        for attr in model.attributes:
            flag = " [changes over time: current value only, never group past periods by it]"
            lines.append(
                f"{attr.name} = {attr.table}.{attr.column}{flag if attr.time_varying else ''}: "
                f"{_values(con, attr.table, attr.column)}"
            )

    lines += ["", "METRICS (aggregate SQL over the joined rows; unit)"]
    for metric in available_metrics(model):
        lines.append(f"{metric.name} = {sql_expr(model, metric)}; {metric.unit}")

    lines += ["", "RULES", RULES]
    return "\n".join(lines)
