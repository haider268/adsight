"""The schema card: the compact, deterministic description of a dataset the LLM sees.

It is resent on every call, so it is terse (one line per item) and byte-identical for
every question, which lets providers cache it as a shared prompt prefix.
"""

import duckdb

from adsight.generate.context import DatasetContext
from adsight.semantic.metrics import available_metrics, sql_expr
from adsight.semantic.model import Level, SemanticModel
from adsight.semantic.sql import from_clause

MAX_ATTRIBUTE_VALUES = 12
MAX_ENTITY_EXAMPLES = 5

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
    "guess, such as which account, metric or period; otherwise answer."
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
    rows = con.execute(f"DESCRIBE {_q(table)}").fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _values(con: duckdb.DuckDBPyConnection, table: str, column: str) -> str:
    rows = con.execute(
        f"SELECT DISTINCT CAST({_q(column)} AS VARCHAR) AS v FROM {_q(table)} "
        f"WHERE {_q(column)} IS NOT NULL ORDER BY v LIMIT {MAX_ATTRIBUTE_VALUES + 1}"
    ).fetchall()
    values = [str(r[0]) for r in rows]
    more = len(values) > MAX_ATTRIBUTE_VALUES
    return " | ".join(values[:MAX_ATTRIBUTE_VALUES]) + (" | ..." if more else "")


def entity_line(model: SemanticModel, level: Level, con: duckdb.DuckDBPyConnection) -> str:
    """`ad (54): "first label", ...`: the level's entity count and its first examples in
    key order (labels, or keys when the level has no label)."""
    key = _q(level.key)
    shown = _q(level.label) if level.label else key
    rows = con.execute(
        f"SELECT {key}, MIN(CAST({shown} AS VARCHAR)), COUNT(*) OVER () FROM {_q(level.table)} "
        f"WHERE {key} IS NOT NULL GROUP BY {key} ORDER BY {key} LIMIT {MAX_ENTITY_EXAMPLES + 1}"
    ).fetchall()
    count = int(rows[0][2]) if rows else 0
    examples = ", ".join(f'"{r[1]}"' for r in rows[:MAX_ENTITY_EXAMPLES])
    more = ", ..." if count > MAX_ENTITY_EXAMPLES else ""
    return f"{level.name} ({count}): {examples}{more}"


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
        lines.append(f"{table}{role}: {items}")

    lines += ["", "HIERARCHY: " + " > ".join(lv.name for lv in model.levels)]
    lines.append("ENTITIES (count: first names in key order)")
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
