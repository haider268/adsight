"""Deterministic sanity checks on a query result. Each issue is one short sentence."""

from decimal import Decimal

from adsight.sql_safety import SqlResult

RATIO_MARKERS = ("cpl", "cost_per", "cpc", "cpm")
RATE_MARKERS = ("ctr", "cvr", "rate")
PERCENT_MARKERS = ("pct", "percent")
MAX_RATIO = 10_000.0
MAX_RATE = 1.0


def _numbers(rows: list[tuple[object, ...]], index: int) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = row[index]
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def check_result(goal: str, result: SqlResult) -> list[str]:
    """Issues with a step's result; an empty list means it looks fine. `goal` names the
    step in nothing but the caller's prompt, so the checks look at the data only."""
    if result.error is not None:
        return [f"The query failed: {result.error}."]
    if not result.rows:
        return ["The query returned no rows."]
    issues: list[str] = []
    for i, column in enumerate(result.columns):
        name = column.lower()
        if all(row[i] is None for row in result.rows):
            issues.append(f"Column {column} is NULL in every row.")
            continue
        values = _numbers(result.rows, i)
        if ("spend" in name or "cost" in name) and any(v < 0 for v in values):
            issues.append(f"Column {column} has a negative value.")
        if any(m in name for m in RATIO_MARKERS) and any(v > MAX_RATIO for v in values):
            issues.append(f"Column {column} is above 10,000, too high for a cost ratio.")
        is_percent = any(m in name for m in PERCENT_MARKERS)
        if (
            any(m in name for m in RATE_MARKERS)
            and not is_percent
            and any(v > MAX_RATE for v in values)
        ):
            issues.append(f"Column {column} is above 1.0, but a rate should be a fraction.")
    return issues
