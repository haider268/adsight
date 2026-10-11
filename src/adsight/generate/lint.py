"""The question linter: the benchmark's question-quality rules, applied as code.

Rules run in order and the first failure rejects the candidate:
R1 executes, R2 volume, R3 margin, R4 definition, R5 trend, R6 distinct, R7 coverage,
R8 duplicate. Ambiguous and unanswerable candidates skip R1-R7.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import duckdb

from adsight.benchmark.questions import Unit
from adsight.benchmark.score import matches
from adsight.generate.context import DatasetContext
from adsight.generate.templates import Candidate, LintSpec
from adsight.generate.wording import is_additive
from adsight.semantic.metrics import METRICS, sql_expr
from adsight.semantic.model import MeasureName, SemanticModel
from adsight.semantic.sql import date_col, measure_col
from adsight.tuning import Tuning


@dataclass(frozen=True)
class LintResult:
    ok: bool
    rule: str | None
    reason: str


def _ok() -> LintResult:
    return LintResult(True, None, "")


def _reject(rule: str, reason: str) -> LintResult:
    return LintResult(False, rule, reason)


def _num(value: Any) -> float:
    return float(value) if isinstance(value, Decimal | int | float) else float(str(value))


def _key(value: Any) -> str:
    return str(value)


def _dsum(model: SemanticModel, measure: MeasureName) -> str:
    if measure not in model.fact.measures:
        return "NULL"
    return f"CAST(SUM({measure_col(model, measure)}) AS DOUBLE)"


@dataclass(frozen=True)
class _Scope:
    key: str
    conversions: float | None
    impressions: float | None
    clicks: float | None
    days: int
    value: float | None


def _scopes(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, spec: LintSpec
) -> dict[str, _Scope]:
    group = spec.group_expr or "'all'"
    where = " AND ".join(spec.where)
    rows = con.execute(
        f"SELECT {group} AS g, {_dsum(model, 'conversions')}, {_dsum(model, 'impressions')}, "
        f"{_dsum(model, 'clicks')}, COUNT(DISTINCT {date_col(model)}), "
        f"{sql_expr(model, spec.metric)} {spec.from_sql} WHERE {where} GROUP BY 1"
    ).fetchall()
    return {
        _key(r[0]): _Scope(
            _key(r[0]),
            None if r[1] is None else float(r[1]),
            None if r[2] is None else float(r[2]),
            None if r[3] is None else float(r[3]),
            int(r[4]),
            None if r[5] is None else _num(r[5]),
        )
        for r in rows
    }


def _volume_problem(spec: LintSpec, scope: _Scope, t: Tuning) -> str | None:
    metric = spec.metric
    point = spec.kind == "trend"
    if metric in ("cpl", "cvr"):
        need = t.min_conversions_per_point if point else t.min_conversions
        if (scope.conversions or 0) < need:
            return f"{scope.key}: {scope.conversions or 0:g} conversions < {need}"
    elif metric in ("ctr", "cpm", "frequency"):
        if (scope.impressions or 0) < t.min_impressions:
            return f"{scope.key}: {scope.impressions or 0:g} impressions < {t.min_impressions}"
    elif metric == "cpc":
        if (scope.clicks or 0) < t.min_clicks:
            return f"{scope.key}: {scope.clicks or 0:g} clicks < {t.min_clicks}"
    elif is_additive(metric):
        return _additive_volume_problem(spec, scope, t)
    return None


def _additive_volume_problem(spec: LintSpec, scope: _Scope, t: Tuning) -> str | None:
    """Additive metrics need real volume: a point or member may be smaller than a scope."""
    metric, value = spec.metric, scope.value or 0.0
    member = spec.kind in ("trend", "comparison")
    if metric == "conversions":
        need = t.min_conversions_per_point if member else t.min_conversions
    elif metric in ("clicks", "link_clicks"):
        need = t.min_clicks_per_point if member else t.min_clicks
    elif metric == "impressions":
        need = t.min_impressions
    else:  # spend
        return None if value > 0 else f"{scope.key}: {metric} is {value:g}, not > 0"
    if value < need:
        return f"{scope.key}: {value:g} {metric} < {need}"
    return None


def _date_denominators(
    con: duckdb.DuckDBPyConnection, model: SemanticModel, spec: LintSpec
) -> dict[str, int]:
    """Distinct fact dates in the period: per time group, or one total under key '*'."""
    fact = '"' + model.fact.table.replace('"', '""') + '"'
    between = (
        f"{date_col(model)} BETWEEN DATE '{spec.start.isoformat()}' "
        f"AND DATE '{spec.end.isoformat()}'"
    )
    if spec.time_groups and spec.group_expr is not None:
        rows = con.execute(
            f"SELECT {spec.group_expr}, COUNT(DISTINCT {date_col(model)}) FROM {fact} f "
            f"WHERE {between} GROUP BY 1"
        ).fetchall()
        return {_key(r[0]): int(r[1]) for r in rows}
    row = con.execute(
        f"SELECT COUNT(DISTINCT {date_col(model)}) FROM {fact} f WHERE {between}"
    ).fetchone()
    return {"*": int(row[0]) if row else 0}


def _alternative_expr(model: SemanticModel, metric: str) -> str | None:
    measures = model.fact.measures
    if "clicks" not in measures or "link_clicks" not in measures:
        return None
    if metric == "ctr":
        return f"{_dsum(model, 'link_clicks')} / NULLIF({_dsum(model, 'impressions')}, 0)"
    if metric == "cvr":
        return f"{_dsum(model, 'conversions')} / NULLIF({_dsum(model, 'clicks')}, 0)"
    return None


def lint(
    candidate: Candidate,
    con: duckdb.DuckDBPyConnection,
    model: SemanticModel,
    ctx: DatasetContext,
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] | None = None,
) -> LintResult:
    """Check one candidate. Pass `seen` (accepted (template, params) keys) to apply R8."""
    del ctx  # the context is fixed into the candidate's SQL already
    if candidate.sql is not None and candidate.spec is not None:
        result = _lint_sql(candidate.sql, candidate.spec, con, model)
        if not result.ok:
            return result
    if seen is not None:
        key = (candidate.template, tuple(sorted(candidate.params.items())))
        if key in seen:
            return _reject("R8_DUPLICATE", "same template and params already accepted")
    return _ok()


def _lint_sql(
    sql: str, spec: LintSpec, con: duckdb.DuckDBPyConnection, model: SemanticModel
) -> LintResult:
    t = model.tuning
    # R1: runs, returns rows, no NULLs
    try:
        rows = con.execute(sql).fetchall()
    except duckdb.Error as exc:
        return _reject("R1_EXECUTES", f"SQL failed: {exc}")
    if not rows:
        return _reject("R1_EXECUTES", "no rows returned")
    if any(v is None for row in rows for v in row):
        return _reject("R1_EXECUTES", "result contains NULL values")

    # which scopes the answer depends on
    if spec.kind == "lookup":
        scope_keys = ["all"]
    elif spec.kind == "aggregation":
        scope_keys = [_key(r[0]) for r in rows[:2]]
    else:
        scope_keys = [_key(r[0]) for r in rows]
    scopes = _scopes(con, model, spec)

    # R2: volume
    for key in scope_keys:
        scope = scopes.get(key)
        if scope is None:
            return _reject("R2_VOLUME", f"{key}: no rows in scope")
        problem = _volume_problem(spec, scope, t)
        if problem:
            return _reject("R2_VOLUME", problem)
    if spec.per_day and spec.metric == "conversions":
        average = _num(rows[0][0])
        if average < t.min_daily_conversions:
            return _reject(
                "R2_VOLUME",
                f"average {average:.3g} conversions per day < {t.min_daily_conversions:g}",
            )

    ratio = not is_additive(spec.metric)
    if spec.kind == "aggregation":
        # R3: margin between winner and runner-up
        if len(rows) < 2:
            return _reject("R3_MARGIN", "fewer than 2 rows to rank")
        top, second = _num(rows[0][2]), _num(rows[1][2])
        need = t.margin_ratio if ratio else t.margin_additive
        margin = abs(top - second) / abs(second) if second != 0 else (0.0 if top == 0 else 1.0)
        if margin < need:
            return _reject(
                "R3_MARGIN",
                f"top {top:.6g} vs second {second:.6g}: margin {margin:.2%} < {need:.0%}",
            )
        # R4: the winner must not depend on the metric's definition
        alt = _alternative_expr(model, spec.metric)
        if alt is not None and spec.group_expr is not None:
            order = "DESC" if spec.direction == "highest" else "ASC"
            alt_row = con.execute(
                f"SELECT {spec.group_expr} AS g, {alt} AS v {spec.from_sql} "
                f"WHERE {' AND '.join(spec.where)} GROUP BY 1 ORDER BY v {order} NULLS LAST, g "
                "LIMIT 1"
            ).fetchone()
            if alt_row is None or _key(alt_row[0]) != _key(rows[0][0]):
                other = None if alt_row is None else _key(alt_row[0])
                return _reject(
                    "R4_DEFINITION",
                    f"winner {_key(rows[0][0])} becomes {other} under the alternative definition",
                )

    if spec.kind == "trend":
        # R5: a clear direction, or clearly flat
        first, last = _num(rows[0][1]), _num(rows[-1][1])
        if first == 0:
            return _reject("R5_TREND", "first value is 0")
        change = (last - first) / first * 100
        if t.trend_max_flat < abs(change) < t.trend_min_change:
            return _reject(
                "R5_TREND",
                f"change {change:+.2f}% is neither flat (<= {t.trend_max_flat:g}%) "
                f"nor clear (>= {t.trend_min_change:g}%)",
            )

    if spec.kind == "comparison":
        # R6: every member value distinguishable from every other
        distinct = distinct_problem(
            {_key(r[0]): _num(r[1]) for r in rows}, METRICS[spec.metric].unit
        )
        if not distinct.ok:
            return distinct

    # R7: coverage of every scope the answer depends on; a missing member or point
    # has no data at all
    if spec.expected_groups is not None and len(rows) != spec.expected_groups:
        return _reject(
            "R7_COVERAGE",
            f"{len(rows)} of {spec.expected_groups} members or points have data",
        )
    denominators = _date_denominators(con, model, spec)
    for key in scope_keys:
        total = denominators.get(key if spec.time_groups else "*", 0)
        days = scopes[key].days
        if total == 0 or days / total < t.min_coverage:
            return _reject("R7_COVERAGE", f"{key}: data on {days} of {total} dates")
    return _ok()


def distinct_problem(values: dict[str, float], unit: Unit) -> LintResult:
    """R6: no two member values may match each other, in either direction."""
    items = list(values.items())
    for i, (la, va) in enumerate(items):
        for lb, vb in items[i + 1 :]:
            if matches(va, vb, unit) or matches(vb, va, unit):
                return _reject("R6_DISTINCT", f"{la} ({va:.6g}) and {lb} ({vb:.6g}) match")
    return _ok()
