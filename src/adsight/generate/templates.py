"""Question templates. Each enumerates candidate slots, renders text and builds reference SQL.

All SQL comes from `adsight.semantic.sql` and `adsight.semantic.metrics`, never from raw
column names, so the same templates work on any dataset with a semantic model. Metric
values are left unrounded: rounding is the scorer's job.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, Literal

import duckdb

from adsight.benchmark.questions import Category
from adsight.generate.context import DatasetContext
from adsight.generate.periods import (
    Period,
    all_periods,
    day_text,
    full_months,
    month_before_start,
    week_starts,
)
from adsight.generate.wording import (
    attribute_display,
    entity_text,
    is_additive,
    level_display,
    metric_display,
)
from adsight.semantic.inspect import normalise
from adsight.semantic.metrics import METRICS, available_metrics, sql_expr
from adsight.semantic.model import SemanticModel
from adsight.semantic.sql import attribute_col, date_col, from_clause, level_key, level_label

LintKind = Literal["lookup", "aggregation", "comparison", "trend"]
Params = dict[str, str]


@dataclass(frozen=True)
class LintSpec:
    """What the linter needs to know about a SQL candidate's scopes."""

    kind: LintKind
    metric: str
    from_sql: str
    where: tuple[str, ...]
    group_expr: str | None  # the expression the answer's rows are keyed by
    time_groups: bool  # groups are slices of time (months, weeks, weekdays)
    start: date
    end: date
    direction: str | None = None  # "highest" / "lowest" for aggregations


@dataclass(frozen=True)
class Candidate:
    template: str
    category: Category
    question: str
    scoring: dict[str, Any]
    params: Params
    sql: str | None = None
    notes: str = ""
    spec: LintSpec | None = None


@dataclass
class Env:
    """Everything templates need to know about one dataset, read once."""

    model: SemanticModel
    ctx: DatasetContext
    periods: dict[str, Period]
    months: list[Period]
    week_starts: list[date]
    metrics: list[str]
    entities: dict[str, list[tuple[str, str]]]  # level -> [(key, label)], ordered by key
    parent_of: dict[str, dict[str, dict[str, str]]]  # level -> key -> {higher level: key}
    attribute_values: dict[str, list[str]]
    columns: list[str]  # every normalised column name in the database
    top_entity: tuple[str, str]  # top-level entity with the most spend
    labels: dict[str, dict[str, str]] = field(default_factory=dict)

    def label(self, level: str, key: str) -> str:
        return self.labels[level][key]

    def levels(self) -> list[str]:
        return [lv.name for lv in self.model.levels]


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _between(env: Env, start: date, end: date) -> str:
    return f"{date_col(env.model)} BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'"


def render_sql(
    select: list[str],
    from_sql: str,
    where: list[str],
    group: str | None = None,
    order: str | None = None,
) -> str:
    lines = ["SELECT " + ",\n       ".join(select), from_sql]
    for i, cond in enumerate(where):
        lines.append(("WHERE " if i == 0 else "  AND ") + cond)
    if group:
        lines.append(f"GROUP BY {group}")
    if order:
        lines.append(f"ORDER BY {order}")
    return "\n".join(lines) + ";\n"


def build_env(model: SemanticModel, con: duckdb.DuckDBPyConnection, ctx: DatasetContext) -> Env:
    level_names = [lv.name for lv in model.levels]
    entities: dict[str, list[tuple[str, str]]] = {}
    labels: dict[str, dict[str, str]] = {}
    for name in level_names:
        rows = con.execute(
            f"SELECT DISTINCT {level_key(model, name)}, {level_label(model, name)} "
            f"{from_clause(model, [name], [])} "
            f"WHERE {level_key(model, name)} IS NOT NULL ORDER BY 1"
        ).fetchall()
        entities[name] = [(str(k), str(lab)) for k, lab in rows]
        labels[name] = dict(entities[name])

    keys = ", ".join(level_key(model, n) for n in level_names)
    paths = con.execute(
        f"SELECT DISTINCT {keys} {from_clause(model, level_names, [])} ORDER BY {keys}"
    ).fetchall()
    parent_of: dict[str, dict[str, dict[str, str]]] = {n: {} for n in level_names}
    for path in paths:
        for i, name in enumerate(level_names):
            if path[i] is None:
                continue
            parent_of[name][str(path[i])] = {
                level_names[j]: str(path[j]) for j in range(i) if path[j] is not None
            }

    attribute_values: dict[str, list[str]] = {}
    for attr in model.attributes:
        col = attribute_col(model, attr.name)
        rows = con.execute(
            f"SELECT DISTINCT {col} {from_clause(model, [], [attr.name])} "
            f"WHERE {col} IS NOT NULL ORDER BY 1"
        ).fetchall()
        attribute_values[attr.name] = [str(r[0]) for r in rows]

    columns = [
        normalise(str(r[0]))
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
        ).fetchall()
    ]

    top = level_names[0]
    row = con.execute(
        f"SELECT {level_key(model, top)}, {level_label(model, top)} "
        f"{from_clause(model, [top], [])} GROUP BY 1, 2 "
        f"ORDER BY {sql_expr(model, 'spend')} DESC, 1 LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("the fact table has no rows")

    periods = all_periods(ctx)
    return Env(
        model=model,
        ctx=ctx,
        periods={p.key: p for p in periods},
        months=full_months(ctx),
        week_starts=week_starts(ctx),
        metrics=[m.name for m in available_metrics(model)],
        entities=entities,
        parent_of=parent_of,
        attribute_values=attribute_values,
        columns=columns,
        top_entity=(str(row[0]), str(row[1])),
        labels=labels,
    )


def _notes(*periods: Period) -> str:
    for p in periods:
        if p.kind == "last30":
            return f"last 30 days = {p.start.isoformat()} to {p.end.isoformat()}"
    return ""


def _unit(metric: str) -> str:
    return METRICS[metric].unit


def _additive(env: Env) -> list[str]:
    return [m for m in env.metrics if is_additive(m)]


def _ratios(env: Env) -> list[str]:
    return [m for m in env.metrics if not is_additive(m)]


def _children(env: Env, level: str, parent_level: str, parent_key: str) -> list[tuple[str, str]]:
    return [
        (k, lab)
        for k, lab in env.entities[level]
        if env.parent_of[level].get(k, {}).get(parent_level) == parent_key
    ]


def _consecutive_months(env: Env, size: int) -> list[list[Period]]:
    """Runs of `size` full months with no calendar gap between them."""
    runs: list[list[Period]] = []
    for i in range(len(env.months) - size + 1):
        run = env.months[i : i + size]
        if all((b.start - a.end).days == 1 for a, b in pairwise(run)):
            runs.append(run)
    return runs


# --- lookups -----------------------------------------------------------------


def _lookup(env: Env, template: str, params: Params, select_expr: str, text: str) -> Candidate:
    model = env.model
    metric, period = params["metric"], env.periods[params["period"]]
    if "level" in params:
        level, key = params["level"], params["entity"]
        from_sql = from_clause(model, [level], [])
        where = [
            f"{level_key(model, level)} = {_lit(key)}",
            _between(env, period.start, period.end),
        ]
    else:
        from_sql = from_clause(model, [], [])
        where = [_between(env, period.start, period.end)]
    sql = render_sql([f"{select_expr} AS {metric}"], from_sql, where)
    spec = LintSpec("lookup", metric, from_sql, tuple(where), None, False, period.start, period.end)
    return Candidate(
        template=template,
        category="lookup",
        question=text,
        scoring={"type": "numeric", "unit": _unit(metric)},
        params=params,
        sql=sql,
        notes=_notes(period),
        spec=spec,
    )


def _entity_slots(env: Env, metrics: list[str]) -> list[Params]:
    return [
        {"metric": m, "level": level, "entity": key, "period": p}
        for m in metrics
        for level in env.levels()
        for key, _ in env.entities[level]
        for p in env.periods
    ]


def l_total_slots(env: Env) -> list[Params]:
    return _entity_slots(env, _additive(env))


def l_total_build(env: Env, params: Params) -> Candidate:
    m, lv, key = params["metric"], params["level"], params["entity"]
    text = (
        f"What was the total {metric_display(m, env.model, env.ctx)} for "
        f"{entity_text(lv, env.label(lv, key))} in {env.periods[params['period']].text}?"
    )
    return _lookup(env, "L_TOTAL", params, sql_expr(env.model, m), text)


def l_ratio_slots(env: Env) -> list[Params]:
    return _entity_slots(env, _ratios(env))


def l_ratio_build(env: Env, params: Params) -> Candidate:
    m, lv, key = params["metric"], params["level"], params["entity"]
    text = (
        f"What was the {metric_display(m, env.model, env.ctx)} for "
        f"{entity_text(lv, env.label(lv, key))} in {env.periods[params['period']].text}?"
    )
    return _lookup(env, "L_RATIO", params, sql_expr(env.model, m), text)


def l_all_slots(env: Env) -> list[Params]:
    return [{"metric": m, "period": p} for m in _additive(env) for p in env.periods]


def l_all_build(env: Env, params: Params) -> Candidate:
    m = params["metric"]
    top_plural = level_display(env.levels()[0], plural=True)
    text = (
        f"What was the total {metric_display(m, env.model, env.ctx)} across all {top_plural} "
        f"in {env.periods[params['period']].text}?"
    )
    return _lookup(env, "L_ALL", params, sql_expr(env.model, m), text)


def l_daily_slots(env: Env) -> list[Params]:
    if env.ctx.grain != "day":
        return []
    return _entity_slots(env, _additive(env))


def l_daily_build(env: Env, params: Params) -> Candidate:
    m, lv, key = params["metric"], params["level"], params["entity"]
    text = (
        f"What was the average daily {metric_display(m, env.model, env.ctx)} for "
        f"{entity_text(lv, env.label(lv, key))} in {env.periods[params['period']].text}?"
    )
    expr = f"CAST({sql_expr(env.model, m)} AS DOUBLE) / COUNT(DISTINCT {date_col(env.model)})"
    return _lookup(env, "L_DAILY", params, expr, text)


# --- aggregations ------------------------------------------------------------


def _ranking(
    env: Env,
    template: str,
    params: Params,
    text: str,
    key_expr: str,
    label_expr: str,
    from_sql: str,
    where: list[str],
    start: date,
    end: date,
    time_groups: bool,
    notes: str = "",
) -> Candidate:
    metric, direction = params["metric"], params["direction"]
    order = "DESC" if direction == "highest" else "ASC"
    sql = render_sql(
        [f"{key_expr} AS key", f"{label_expr} AS label", f"{sql_expr(env.model, metric)} AS value"],
        from_sql,
        where,
        group="1, 2",
        order=f"value {order}, key",
    )
    spec = LintSpec(
        "aggregation", metric, from_sql, tuple(where), key_expr, time_groups, start, end, direction
    )
    return Candidate(
        template=template,
        category="aggregation",
        question=text,
        scoring={"type": "entity"},
        params=params,
        sql=sql,
        notes=notes,
        spec=spec,
    )


def a_rank_slots(env: Env) -> list[Params]:
    slots: list[Params] = []
    names = env.levels()
    for li, level in enumerate(names):
        scopes: list[dict[str, str]] = []
        if len(env.entities[level]) >= 2:
            scopes.append({})
        for higher in names[:li]:
            for pkey, _ in env.entities[higher]:
                if len(_children(env, level, higher, pkey)) >= 2:
                    scopes.append({"scope_level": higher, "scope_entity": pkey})
        for scope in scopes:
            for m in env.metrics:
                for direction in ("highest", "lowest"):
                    for p in env.periods:
                        slots.append(
                            {"metric": m, "level": level, "direction": direction, "period": p}
                            | scope
                        )
    return slots


def a_rank_build(env: Env, params: Params) -> Candidate:
    model, level = env.model, params["level"]
    period = env.periods[params["period"]]
    levels = [level]
    where = [_between(env, period.start, period.end)]
    scope_text = ""
    if "scope_level" in params:
        higher, pkey = params["scope_level"], params["scope_entity"]
        levels.append(higher)
        where.append(f"{level_key(model, higher)} = {_lit(pkey)}")
        scope_text = f" in {entity_text(higher, env.label(higher, pkey))}"
    text = (
        f"Which {level_display(level)}{scope_text} had the {params['direction']} "
        f"{metric_display(params['metric'], model, env.ctx)} in {period.text}?"
    )
    return _ranking(
        env,
        "A_RANK",
        params,
        text,
        level_key(model, level),
        level_label(model, level),
        from_clause(model, levels, []),
        where,
        period.start,
        period.end,
        time_groups=False,
        notes=_notes(period),
    )


def a_attr_slots(env: Env) -> list[Params]:
    return [
        {"attribute": a, "metric": m, "direction": d, "period": p}
        for a, values in env.attribute_values.items()
        if len(values) >= 2
        for m in env.metrics
        for d in ("highest", "lowest")
        for p in env.periods
    ]


def a_attr_build(env: Env, params: Params) -> Candidate:
    model, attr = env.model, params["attribute"]
    period = env.periods[params["period"]]
    col = attribute_col(model, attr)
    text = (
        f"Which {attribute_display(attr)} had the {params['direction']} "
        f"{metric_display(params['metric'], model, env.ctx)} in {period.text}?"
    )
    return _ranking(
        env,
        "A_ATTR",
        params,
        text,
        col,
        col,
        from_clause(model, [], [attr]),
        [_between(env, period.start, period.end)],
        period.start,
        period.end,
        time_groups=False,
        notes=_notes(period),
    )


def _month_expr(env: Env) -> str:
    return f"strftime({date_col(env.model)}, '%Y-%m')"


def a_month_slots(env: Env) -> list[Params]:
    if len(env.months) < 2:
        return []
    return [{"metric": m, "direction": d} for m in _additive(env) for d in ("highest", "lowest")]


def a_month_build(env: Env, params: Params) -> Candidate:
    model = env.model
    first, last = env.months[0], env.months[-1]
    expr = _month_expr(env)
    keys = ", ".join(_lit(p.key) for p in env.months)
    text = (
        f"Which full calendar month had the {params['direction']} total "
        f"{metric_display(params['metric'], model, env.ctx)}?"
    )
    return _ranking(
        env,
        "A_MONTH",
        params,
        text,
        expr,
        expr,
        from_clause(model, [], []),
        [_between(env, first.start, last.end), f"{expr} IN ({keys})"],
        first.start,
        last.end,
        time_groups=True,
    )


def a_weekday_slots(env: Env) -> list[Params]:
    if env.ctx.grain != "day":
        return []
    return [{"metric": "conversions", "direction": d} for d in ("highest", "lowest")]


def a_weekday_build(env: Env, params: Params) -> Candidate:
    model = env.model
    word = "most" if params["direction"] == "highest" else "fewest"
    top_plural = level_display(env.levels()[0], plural=True)
    expr = f"dayname({date_col(model)})"
    whole = env.periods["whole"]
    text = (
        f"Which day of the week brings in the {word} {env.ctx.conversion_plural} "
        f"across all {top_plural}?"
    )
    return _ranking(
        env,
        "A_WEEKDAY",
        params,
        text,
        expr,
        expr,
        from_clause(model, [], []),
        [_between(env, whole.start, whole.end)],
        whole.start,
        whole.end,
        time_groups=True,
    )


# --- comparisons -------------------------------------------------------------


def _comparison(
    env: Env,
    template: str,
    params: Params,
    text: str,
    label_expr: str,
    from_sql: str,
    where: list[str],
    start: date,
    end: date,
    time_groups: bool,
    notes: str = "",
) -> Candidate:
    metric = params["metric"]
    sql = render_sql(
        [f"{label_expr} AS label", f"{sql_expr(env.model, metric)} AS value"],
        from_sql,
        where,
        group="1",
        order="1",
    )
    spec = LintSpec(
        "comparison", metric, from_sql, tuple(where), label_expr, time_groups, start, end
    )
    return Candidate(
        template=template,
        category="comparison",
        question=text,
        scoring={"type": "numeric_set", "unit": _unit(metric)},
        params=params,
        sql=sql,
        notes=notes,
        spec=spec,
    )


def _distinct_labels(members: list[tuple[str, str]]) -> bool:
    labels = [lab for _, lab in members]
    return len(labels) == len(set(labels))


def c_level_slots(env: Env) -> list[Params]:
    names = env.levels()
    groups: list[dict[str, str]] = []
    top = env.entities[names[0]]
    if 2 <= len(top) <= 6 and _distinct_labels(top):
        groups.append({"level": names[0]})
    for li in range(1, len(names)):
        level, parent = names[li], names[li - 1]
        for pkey, _ in env.entities[parent]:
            members = _children(env, level, parent, pkey)
            if 2 <= len(members) <= 6 and _distinct_labels(members):
                groups.append({"level": level, "parent_level": parent, "parent_entity": pkey})
    return [
        g | {"metric": m, "period": p} for g in groups for m in env.metrics for p in env.periods
    ]


def c_level_build(env: Env, params: Params) -> Candidate:
    model, level = env.model, params["level"]
    period = env.periods[params["period"]]
    metric_text = metric_display(params["metric"], model, env.ctx)
    levels = [level]
    where = [_between(env, period.start, period.end)]
    if "parent_level" in params:
        parent, pkey = params["parent_level"], params["parent_entity"]
        levels.append(parent)
        where.append(f"{level_key(model, parent)} = {_lit(pkey)}")
        parent_text = entity_text(parent, env.label(parent, pkey))
        scope = f"the {level_display(level, plural=True)} in {parent_text}"
    else:
        scope = f"all {level_display(level, plural=True)}"
    text = f"Compare {metric_text} across {scope} in {period.text}."
    return _comparison(
        env,
        "C_LEVEL",
        params,
        text,
        level_label(model, level),
        from_clause(model, levels, []),
        where,
        period.start,
        period.end,
        time_groups=False,
        notes=_notes(period),
    )


def c_attr_slots(env: Env) -> list[Params]:
    return [
        {"attribute": a, "metric": m, "period": p}
        for a, values in env.attribute_values.items()
        if 2 <= len(values) <= 6
        for m in env.metrics
        for p in env.periods
    ]


def c_attr_build(env: Env, params: Params) -> Candidate:
    model, attr = env.model, params["attribute"]
    period = env.periods[params["period"]]
    text = (
        f"Compare {metric_display(params['metric'], model, env.ctx)} by "
        f"{attribute_display(attr)} in {period.text}."
    )
    return _comparison(
        env,
        "C_ATTR",
        params,
        text,
        attribute_col(model, attr),
        from_clause(model, [], [attr]),
        [_between(env, period.start, period.end)],
        period.start,
        period.end,
        time_groups=False,
        notes=_notes(period),
    )


def c_months_slots(env: Env) -> list[Params]:
    slots: list[Params] = []
    for size in range(3, 7):
        for run in _consecutive_months(env, size):
            for m in _additive(env):
                for level in env.levels():
                    for key, _ in env.entities[level]:
                        slots.append(
                            {
                                "metric": m,
                                "level": level,
                                "entity": key,
                                "first": run[0].key,
                                "last": run[-1].key,
                            }
                        )
    return slots


def _month_run(env: Env, first: str, last: str) -> list[Period]:
    keys = [p.key for p in env.months]
    return env.months[keys.index(first) : keys.index(last) + 1]


def c_months_build(env: Env, params: Params) -> Candidate:
    model, level, key = env.model, params["level"], params["entity"]
    run = _month_run(env, params["first"], params["last"])
    text = (
        f"Compare monthly {metric_display(params['metric'], model, env.ctx)} for "
        f"{entity_text(level, env.label(level, key))} from {run[0].text} to {run[-1].text}."
    )
    return _comparison(
        env,
        "C_MONTHS",
        params,
        text,
        _month_expr(env),
        from_clause(model, [level], []),
        [f"{level_key(model, level)} = {_lit(key)}", _between(env, run[0].start, run[-1].end)],
        run[0].start,
        run[-1].end,
        time_groups=True,
    )


# --- trends ------------------------------------------------------------------

_TREND_TAIL = " Is it rising, falling, or roughly flat?"


def _trend(
    env: Env,
    template: str,
    params: Params,
    text: str,
    period_expr: str,
    start: date,
    end: date,
) -> Candidate:
    model, level, key, metric = env.model, params["level"], params["entity"], params["metric"]
    from_sql = from_clause(model, [level], [])
    where = [f"{level_key(model, level)} = {_lit(key)}", _between(env, start, end)]
    sql = render_sql(
        [f"{period_expr} AS period", f"{sql_expr(model, metric)} AS value"],
        from_sql,
        where,
        group="1",
        order="1",
    )
    spec = LintSpec("trend", metric, from_sql, tuple(where), period_expr, True, start, end)
    return Candidate(
        template=template,
        category="trend",
        question=text + _TREND_TAIL,
        scoring={"type": "trend", "unit": _unit(metric), "flat_band_pct": 5.0},
        params=params,
        sql=sql,
        spec=spec,
    )


def t_weekly_slots(env: Env) -> list[Params]:
    slots: list[Params] = []
    starts = env.week_starts
    for n in (4, 6, 8):
        for i in range(len(starts) - n + 1):
            for m in env.metrics:
                for level in env.levels():
                    for key, _ in env.entities[level]:
                        slots.append(
                            {
                                "metric": m,
                                "level": level,
                                "entity": key,
                                "start": starts[i].isoformat(),
                                "weeks": str(n),
                            }
                        )
    return slots


def t_weekly_build(env: Env, params: Params) -> Candidate:
    model, level, key = env.model, params["level"], params["entity"]
    start = date.fromisoformat(params["start"])
    end = start + timedelta(days=7 * int(params["weeks"]) - 1)
    text = (
        f"Look at weekly {metric_display(params['metric'], model, env.ctx)} for "
        f"{entity_text(level, env.label(level, key))} from {day_text(start)} to {day_text(end)}."
    )
    period_expr = (
        f"date_trunc('week', {date_col(model)})" if env.ctx.grain == "day" else date_col(model)
    )
    return _trend(env, "T_WEEKLY", params, text, period_expr, start, end)


def t_monthly_slots(env: Env) -> list[Params]:
    slots: list[Params] = []
    for size in range(3, 7):
        for run in _consecutive_months(env, size):
            for m in env.metrics:
                for level in env.levels():
                    for key, _ in env.entities[level]:
                        slots.append(
                            {
                                "metric": m,
                                "level": level,
                                "entity": key,
                                "first": run[0].key,
                                "last": run[-1].key,
                            }
                        )
    return slots


def t_monthly_build(env: Env, params: Params) -> Candidate:
    model, level, key = env.model, params["level"], params["entity"]
    run = _month_run(env, params["first"], params["last"])
    text = (
        f"Look at monthly {metric_display(params['metric'], model, env.ctx)} for "
        f"{entity_text(level, env.label(level, key))} from {run[0].text} to {run[-1].text}."
    )
    period_expr = f"date_trunc('month', {date_col(model)})"
    return _trend(env, "T_MONTHLY", params, text, period_expr, run[0].start, run[-1].end)


# --- ambiguous and unanswerable ----------------------------------------------


def ambiguous_phrasings(env: Env) -> list[tuple[str, str]]:
    """(phrasing key, question) pairs available for this dataset."""
    names = env.levels()
    second = names[1] if len(names) > 1 else names[0]
    out = [
        ("budget_ok", "Is the budget OK?"),
        ("ads_doing", "How are the ads doing?"),
        ("turn_off", "Which ads should I turn off?"),
        ("last_month", "How did we do last month?"),
        ("getting_worse", "Is performance getting worse?"),
        ("our_cpl", f"What's our cost per {env.ctx.conversion_name}?"),
        ("spending_too_much", "Are we spending too much?"),
        ("increase_budget", "Should we increase the budget?"),
    ]
    if "campaign" in names:
        out.append(("compare_campaigns", "Can you compare the campaigns?"))
    out.append(("show_best", f"Show me the best {level_display(second)}."))
    out.append(("entity_doing", f"How is {env.top_entity[1]} doing?"))
    if env.model.change_log is not None:
        out.append(("change_work", "Did the change work?"))
    return out


@dataclass(frozen=True)
class Probe:
    concept: str
    words: tuple[str, ...]
    question: Callable[[Env], str]
    reason: Callable[[Env], str]


def _last_full_month(env: Env) -> str:
    return env.months[-1].text if env.months else "the last month"


PROBES: tuple[Probe, ...] = (
    Probe(
        "revenue",
        ("revenue", "purchase_value", "conversion_value", "roas", "sales_value"),
        lambda e: (
            f"What was the return on ad spend for {e.top_entity[1]} in {_last_full_month(e)}?"
        ),
        lambda e: (
            "There is no revenue or conversion value data, so return on ad spend cannot be "
            "calculated."
        ),
    ),
    Probe(
        "profit",
        ("profit", "margin", "revenue"),
        lambda e: f"What was the profit from {e.top_entity[1]}'s ads over the whole period?",
        lambda e: (
            "There is no revenue, cost of goods or margin data, so profit cannot be calculated."
        ),
    ),
    Probe(
        "customers",
        ("customer_count", "sales", "closed", "won", "patients", "deals"),
        lambda e: (
            f"How many of {e.top_entity[1]}'s {e.ctx.conversion_plural} became paying customers?"
        ),
        lambda e: (
            f"The data stops at {e.ctx.conversion_plural}; there is no record of which became "
            "paying customers."
        ),
    ),
    Probe(
        "demographics",
        ("age", "gender"),
        lambda e: f"Which age group converts best for {e.top_entity[1]}?",
        lambda e: "There is no age or gender breakdown in the data.",
    ),
    Probe(
        "geography",
        ("city", "region", "country", "dma", "postcode", "zip"),
        lambda e: f"Which city brings in the most {e.ctx.conversion_plural}?",
        lambda e: "There is no location data in the data.",
    ),
    Probe(
        "engagement",
        ("comments", "likes", "shares", "reactions"),
        lambda e: "Which ad gets the most comments?",
        lambda e: "There is no engagement data such as comments, likes or shares.",
    ),
    Probe(
        "video",
        ("video_views", "thruplays", "watch_time", "video_p"),
        lambda e: "What was the average video watch time last month?",
        lambda e: "There is no video viewing data.",
    ),
    Probe(
        "device",
        ("device", "platform_position", "os"),
        lambda e: "Do mobile or desktop users convert better?",
        lambda e: "There is no device breakdown in the data.",
    ),
    Probe(
        "hour",
        ("hour", "hour_of_day"),
        lambda e: f"What hour of the day brings in the most {e.ctx.conversion_plural}?",
        lambda e: f"The data has no time of day; the finest grain is one {e.ctx.grain}.",
    ),
    Probe(
        "platform",
        ("platform", "publisher_platform", "network", "channel"),
        lambda e: f"How did the TikTok campaigns perform in {_last_full_month(e)}?",
        lambda e: "There is no platform column and no TikTok campaigns in the data.",
    ),
    Probe(
        "market",
        (),
        lambda e: (
            f"How does our cost per {e.ctx.conversion_name} compare with other advertisers in "
            "the market?"
        ),
        lambda e: "There is no market or competitor data, only this account's own data.",
    ),
    Probe(
        "before_start",
        (),
        lambda e: f"What was the total spend in {month_before_start(e.ctx)}?",
        lambda e: (
            f"The data covers {e.ctx.data_start.isoformat()} to {e.ctx.data_end.isoformat()} "
            "only; that month is outside it."
        ),
    ),
)


def available_probes(env: Env) -> list[Probe]:
    """Probes none of whose words appear inside any column name."""
    return [p for p in PROBES if not any(w in col for w in p.words for col in env.columns)]


def ambiguous_candidate(key: str, text: str) -> Candidate:
    return Candidate(
        template="AMBIGUOUS",
        category="ambiguous",
        question=text,
        scoring={"type": "clarify"},
        params={"phrasing": key},
    )


def unanswerable_candidate(env: Env, probe: Probe) -> Candidate:
    return Candidate(
        template="UNANSWERABLE",
        category="unanswerable",
        question=probe.question(env),
        scoring={"type": "refuse", "reason": probe.reason(env)},
        params={"probe": probe.concept},
    )


# --- registry ----------------------------------------------------------------


@dataclass(frozen=True)
class Template:
    id: str
    category: Category
    slots: Callable[[Env], list[Params]]
    build: Callable[[Env, Params], Candidate]


TEMPLATES: tuple[Template, ...] = (
    Template("L_TOTAL", "lookup", l_total_slots, l_total_build),
    Template("L_RATIO", "lookup", l_ratio_slots, l_ratio_build),
    Template("L_ALL", "lookup", l_all_slots, l_all_build),
    Template("L_DAILY", "lookup", l_daily_slots, l_daily_build),
    Template("A_RANK", "aggregation", a_rank_slots, a_rank_build),
    Template("A_ATTR", "aggregation", a_attr_slots, a_attr_build),
    Template("A_MONTH", "aggregation", a_month_slots, a_month_build),
    Template("A_WEEKDAY", "aggregation", a_weekday_slots, a_weekday_build),
    Template("C_LEVEL", "comparison", c_level_slots, c_level_build),
    Template("C_ATTR", "comparison", c_attr_slots, c_attr_build),
    Template("C_MONTHS", "comparison", c_months_slots, c_months_build),
    Template("T_WEEKLY", "trend", t_weekly_slots, t_weekly_build),
    Template("T_MONTHLY", "trend", t_monthly_slots, t_monthly_build),
)
TEMPLATES_BY_ID = {t.id: t for t in TEMPLATES}


def build_candidate(env: Env, template_id: str, params: Params) -> Candidate:
    return TEMPLATES_BY_ID[template_id].build(env, params)
