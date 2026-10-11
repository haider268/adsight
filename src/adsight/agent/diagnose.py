"""The B4 diagnose tool: deterministic evidence for a "why" question, from raw data views.

It reads only the semantic layer and the database. It deliberately knows nothing about the
benchmark generator's detectors (they are the answer key): it lists logged changes near the
scope, before/after metrics around each change, the scope's daily series, and weekly
frequency and CTR when reach is mapped. It never guesses an entity or a window.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import duckdb

from adsight.log import get_logger
from adsight.semantic.metrics import sql_expr
from adsight.semantic.model import SemanticModel
from adsight.semantic.sql import date_col, from_clause, level_key
from adsight.sql_safety import query

CHANGE_MARGIN_DAYS = 21
COMPARE_DAYS = 14
PRE_WINDOW_DAYS = 14
FREQUENCY_WEEKS = 6
MAX_LINES = 60
MAX_ENTITIES = 3
MAX_CHANGES = 20

log = get_logger("agent.diagnose")

MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", re.I)
_MONTH_YEAR = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{4})\b", re.I)
_QUOTED = re.compile(r'"([^"]+)"')
_SQL_STRING = re.compile(r"'((?:[^']|'')*)'")


@dataclass(frozen=True)
class Entity:
    level: str
    key: str
    label: str


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _entities(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> dict[str, Entity]:
    """Every entity, findable by its label and by its key."""
    index: dict[str, Entity] = {}
    for lv in model.levels:
        shown = _q(lv.label) if lv.label else _q(lv.key)
        rows = query(
            con,
            f"SELECT DISTINCT CAST({_q(lv.key)} AS VARCHAR), CAST({shown} AS VARCHAR) "
            f"FROM {_q(lv.table)} WHERE {_q(lv.key)} IS NOT NULL ORDER BY 1",
        )
        for key, label in rows:
            entity = Entity(lv.name, str(key), str(label))
            index.setdefault(str(key), entity)
            index.setdefault(str(label), entity)
    return index


def find_entities(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    question: str,
    steps: list[dict[str, Any]],
) -> list[Entity]:
    """Entities named in the question (in double quotes) that the plan's SQL or results also
    mention; failing that, the entities the plan's SQL filters on."""
    index = _entities(model, con)
    in_plan: list[str] = []
    for step in steps:
        in_plan += [s.replace("''", "'") for s in _SQL_STRING.findall(step.get("sql", ""))]
        for row in step.get("rows", []):
            in_plan += [str(v) for v in row if isinstance(v, str)]
    plan_names = set(in_plan)
    found: list[Entity] = []
    for name in _QUOTED.findall(question):
        if name in index and name in plan_names and index[name] not in found:
            found.append(index[name])
    if not found:
        for name in in_plan:
            if name in index and index[name] not in found:
                found.append(index[name])
    return found[:MAX_ENTITIES]


def _month_end(year: int, month: int) -> date:
    first_next = date(year + month // 12, month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def find_window(question: str, steps: list[dict[str, Any]]) -> tuple[date, date] | None:
    """The span of dates the plan queried; failing that, the dates the question names."""
    found: list[date | None] = []
    for step in steps:
        found += [_safe_date(y, m, d) for y, m, d in _ISO.findall(step.get("sql", ""))]
    dates = [d for d in found if d is not None]
    if dates:
        return min(dates), max(dates)
    found = [_safe_date(y, m, d) for y, m, d in _ISO.findall(question)]
    found += [
        _safe_date(y, MONTHS[month.lower()], d) for d, month, y in _DAY_MONTH_YEAR.findall(question)
    ]
    dates = [d for d in found if d is not None]
    if not dates:
        for month, y in _MONTH_YEAR.findall(question):
            m = MONTHS[month.lower()]
            dates += [date(int(y), m, 1), _month_end(int(y), m)]
    return (min(dates), max(dates)) if dates else None


def _safe_date(year: str | int, month: str | int, day: str | int) -> date | None:
    """A date, or None for an impossible one such as 2026-02-30 (model-written SQL)."""
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _related_keys(
    model: SemanticModel, con: duckdb.DuckDBPyConnection, entity: Entity
) -> dict[str, set[str]]:
    """level -> keys of the entity itself, its ancestors and its descendants."""
    names = [lv.name for lv in model.levels]
    select = ", ".join(f"CAST({level_key(model, n)} AS VARCHAR)" for n in names)
    rows = query(
        con,
        f"SELECT DISTINCT {select} {from_clause(model, names, [])} "
        f"WHERE CAST({level_key(model, entity.level)} AS VARCHAR) = ?",
        [entity.key],
    )
    related: dict[str, set[str]] = {n: set() for n in names}
    for row in rows:
        for name, value in zip(names, row, strict=True):
            if value is not None:
                related[name].add(str(value))
    related[entity.level].add(entity.key)
    return related


def _changes(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    related: dict[str, set[str]],
    start: date,
    end: date,
) -> list[tuple[Any, ...]]:
    log = model.change_log
    assert log is not None
    old = _q(log.old_value) if log.old_value else "NULL"
    new = _q(log.new_value) if log.new_value else "NULL"
    etype = _q(log.entity_type) if log.entity_type else "NULL"
    rows = query(
        con,
        f"SELECT CAST({_q(log.date)} AS DATE), CAST({etype} AS VARCHAR), "
        f"CAST({_q(log.entity_id)} AS VARCHAR), CAST({_q(log.change_type)} AS VARCHAR), "
        f"CAST({old} AS VARCHAR), CAST({new} AS VARCHAR) FROM {_q(log.table)} "
        f"WHERE CAST({_q(log.date)} AS DATE) BETWEEN ? AND ? ORDER BY 1, 3",
        [start - timedelta(days=CHANGE_MARGIN_DAYS), end + timedelta(days=CHANGE_MARGIN_DAYS)],
    )
    out = []
    for row in rows:
        level, entity_id = row[1], row[2]
        if level is not None and level in related:
            hit = entity_id in related[level]
        else:
            hit = any(entity_id in keys for keys in related.values())
        if hit:
            out.append(row)
    return out


def _metrics(
    model: SemanticModel, con: duckdb.DuckDBPyConnection, level: str, key: str, a: date, b: date
) -> tuple[Any, ...] | None:
    exprs = ", ".join(sql_expr(model, m) for m in ("cpl", "conversions", "clicks", "spend"))
    sql = (
        f"SELECT {exprs} {from_clause(model, [level], [])} "
        f"WHERE CAST({level_key(model, level)} AS VARCHAR) = ? "
        f"AND CAST({date_col(model)} AS DATE) BETWEEN ? AND ?"
    )
    rows = query(con, sql, [key, a, b])
    return rows[0] if rows else None


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(round(number, 2))


def _pct(before: Any, after: Any) -> str:
    if before is None or after is None or float(before) == 0:
        return "n/a"
    change = round((float(after) - float(before)) / float(before) * 100, 1)
    return f"{'+' if change >= 0 else ''}{change!r}%"


def _series(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    entity: Entity,
    a: date,
    b: date,
    metrics: tuple[str, ...],
    bucket: str,
) -> list[tuple[Any, ...]]:
    period = f"date_trunc('{bucket}', CAST({date_col(model)} AS DATE))"
    exprs = ", ".join(sql_expr(model, m) for m in metrics)
    return query(
        con,
        f"SELECT CAST({period} AS DATE) AS p, {exprs} {from_clause(model, [entity.level], [])} "
        f"WHERE CAST({level_key(model, entity.level)} AS VARCHAR) = ? "
        f"AND CAST({date_col(model)} AS DATE) BETWEEN ? AND ? GROUP BY 1 ORDER BY 1",
        [entity.key, a, b],
    )


def diagnose(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    question: str,
    steps: list[dict[str, Any]],
) -> str:
    """Compact evidence text (at most MAX_LINES lines). `steps` carry each plan step's
    `sql` and result `rows`. Never raises: an unexpected failure becomes one line, with
    the traceback logged."""
    try:
        return _diagnose(model, con, question, steps)
    except Exception as exc:
        log.error(
            "diagnose failed",
            exc_info=True,
            extra={"fields": {"error": f"{type(exc).__name__}: {exc}"}},
        )
        return f"Diagnosis unavailable: {type(exc).__name__}"


def _diagnose(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    question: str,
    steps: list[dict[str, Any]],
) -> str:
    entities = find_entities(model, con, question, steps)
    window = find_window(question, steps)
    if not entities:
        return "Diagnosis: cannot determine which entity the question is about."
    if window is None:
        return "Diagnosis: cannot determine the date window the question is about."
    start, end = window
    scope = entities[0]
    names = ", ".join(f'{e.level} {e.key} "{e.label}"' for e in entities)
    lines = [f"Diagnosis for {names}; window {start} to {end}."]

    if model.change_log is None:
        lines.append("Change log: none mapped.")
    else:
        related: dict[str, set[str]] = {}
        for e in entities:
            for level, keys in _related_keys(model, con, e).items():
                related.setdefault(level, set()).update(keys)
        changes = _changes(model, con, related, start, end)
        lines.append(
            f"Logged changes on these entities, their parents and children, {CHANGE_MARGIN_DAYS}"
            f" days either side of the window: {len(changes)}"
        )
        for day, level, entity_id, kind, old, new in changes[:MAX_CHANGES]:
            lines.append(f"- {day} {level or ''} {entity_id} {kind} {old or ''} -> {new or ''}")
            if level is None or level not in [lv.name for lv in model.levels]:
                continue
            before = _metrics(
                model, con, level, entity_id, day - timedelta(COMPARE_DAYS), day - timedelta(1)
            )
            after = _metrics(model, con, level, entity_id, day, day + timedelta(COMPARE_DAYS - 1))
            if before is None or after is None:
                continue
            parts = [
                f"{name} {_fmt(b)} -> {_fmt(a)} ({_pct(b, a)})"
                for name, b, a in zip(
                    ("cpl", "conversions", "clicks", "spend"), before, after, strict=True
                )
            ]
            lines.append(f"  {COMPARE_DAYS} days before vs from the change: " + "; ".join(parts))

    pre = start - timedelta(days=PRE_WINDOW_DAYS)
    bucket = "day" if model.grain == "day" else "week"
    rows = _series(model, con, scope, pre, end, ("conversions", "clicks"), bucket)
    lines.append(
        f"{scope.level} {scope.key} by {bucket} from {pre} to {end} "
        f"({PRE_WINDOW_DAYS} days before the window, then the window):"
    )
    lines.append("conversions: " + " ".join(_fmt(r[1]) for r in rows))
    lines.append("clicks: " + " ".join(_fmt(r[2]) for r in rows))

    if "reach" in model.fact.measures:
        weeks_from = start - timedelta(weeks=FREQUENCY_WEEKS)
        weekly = _series(model, con, scope, weeks_from, end, ("frequency", "ctr"), "week")
        lines.append(f"{scope.level} {scope.key} weekly from {weeks_from} to {end}:")
        lines.append(
            "frequency: "
            + " ".join(repr(round(float(r[1]), 2)) for r in weekly if r[1] is not None)
        )
        lines.append(
            "ctr: " + " ".join(repr(round(float(r[2]), 4)) for r in weekly if r[2] is not None)
        )

    if len(lines) > MAX_LINES:
        lines = [*lines[: MAX_LINES - 1], "(truncated)"]
    return "\n".join(lines)
