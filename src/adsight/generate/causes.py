"""Root-cause detection: logged material changes with a measurable effect, plus strict
pattern detectors (tracking breaks, creative fatigue, calendar effects, noise).

Weekly cost per lead is very noisy at ad-set level, so detection never starts from a
jump. It starts from a logged MATERIAL change and checks the change had an effect, or
from a detector strict enough to have few false positives. Small routine changes are
never causes; they are kept as decoys.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import Literal

import duckdb

from adsight.generate.periods import full_months, week_starts
from adsight.generate.templates import Env
from adsight.semantic.model import MeasureName, SemanticModel
from adsight.semantic.sql import date_col, from_clause, level_key, measure_col

CauseKind = Literal["logged", "tracking", "fatigue", "calendar", "noise"]
Evidence = dict[str, float | str]


@dataclass(frozen=True)
class Cause:
    cause_id: str
    kind: CauseKind
    entity_level: str
    entity_key: str
    entity_label: str
    scope_level: str
    scope_key: str
    scope_label: str
    start: date
    end: date
    evidence: Evidence
    logged: bool
    needs_review: bool


@dataclass(frozen=True)
class Change:
    """One change-log row, placed in the hierarchy."""

    day: date
    level: str
    key: str
    label: str
    change_type: str
    old: str | None
    new: str | None
    material: bool

    def describe(self) -> str:
        """'budget change from 40.00 to 44.00', 'audience change', 'launch', ..."""
        if self.change_type == "budget_change" and self.old and self.new:
            return f"budget change from {self.old} to {self.new}"
        if self.change_type == "launched":
            return "launch"
        return self.change_type.replace("_", " ").replace("edit", "change")


@dataclass
class Detection:
    causes: list[Cause] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    @property
    def decoys(self) -> list[Change]:
        """Clearly irrelevant changes: non-material and not a launch (a new campaign in
        the same account can plausibly matter)."""
        return [c for c in self.changes if not c.material and c.change_type != "launched"]

    def of_kind(self, kind: CauseKind) -> list[Cause]:
        return [c for c in self.causes if c.kind == kind]


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _between(model: SemanticModel, start: date, end: date) -> str:
    return f"{date_col(model)} BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'"


def _sum(model: SemanticModel, measure: MeasureName) -> str:
    if measure not in model.fact.measures:
        return "0"
    return f"COALESCE(CAST(SUM({measure_col(model, measure)}) AS DOUBLE), 0)"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class _Totals:
    spend: float
    conversions: float
    clicks: float

    @property
    def cpl(self) -> float | None:
        return self.spend / self.conversions if self.conversions else None


def _totals(
    model: SemanticModel,
    con: duckdb.DuckDBPyConnection,
    level: str,
    key: str,
    start: date,
    end: date,
) -> _Totals:
    row = con.execute(
        f"SELECT {_sum(model, 'spend')}, {_sum(model, 'conversions')}, {_sum(model, 'clicks')} "
        f"{from_clause(model, [level], [])} "
        f"WHERE {level_key(model, level)} = {_lit(key)} AND {_between(model, start, end)}"
    ).fetchone()
    if row is None:
        return _Totals(0.0, 0.0, 0.0)
    return _Totals(float(row[0]), float(row[1]), float(row[2]))


def _rel(after: float, before: float) -> float:
    return after / before - 1 if before else math.inf


# --- hierarchy helpers -------------------------------------------------------------


def _ancestors(env: Env, level: str, key: str) -> dict[str, str]:
    return env.parent_of.get(level, {}).get(key, {})


def _parent(env: Env, level: str, key: str) -> tuple[str, str] | None:
    names = env.levels()
    i = names.index(level)
    if i == 0:
        return None
    parent_level = names[i - 1]
    parent_key = _ancestors(env, level, key).get(parent_level)
    return (parent_level, parent_key) if parent_key is not None else None


def related(env: Env, level_a: str, key_a: str, level_b: str, key_b: str) -> bool:
    """Same entity, or one is an ancestor of the other."""
    if (level_a, key_a) == (level_b, key_b):
        return True
    if _ancestors(env, level_a, key_a).get(level_b) == key_b:
        return True
    return _ancestors(env, level_b, key_b).get(level_a) == key_a


def _touches(env: Env, cause: Cause, level: str, key: str) -> bool:
    return related(env, cause.entity_level, cause.entity_key, level, key) or related(
        env, cause.scope_level, cause.scope_key, level, key
    )


def _on_entity_or_ancestor(env: Env, change: Change, level: str, key: str) -> bool:
    if (change.level, change.key) == (level, key):
        return True
    return _ancestors(env, level, key).get(change.level) == change.key


def _overlaps(a0: date, a1: date, b0: date, b1: date) -> bool:
    return a0 <= b1 and b0 <= a1


# --- 1. logged changes ---------------------------------------------------------------


def read_changes(env: Env, con: duckdb.DuckDBPyConnection) -> list[Change]:
    log = env.model.change_log
    if log is None:
        return []
    cols = [
        _q(log.date),
        _q(log.entity_type) if log.entity_type else "NULL",
        _q(log.entity_id),
        _q(log.change_type),
        f"CAST({_q(log.old_value)} AS VARCHAR)" if log.old_value else "NULL",
        f"CAST({_q(log.new_value)} AS VARCHAR)" if log.new_value else "NULL",
    ]
    rows = con.execute(f"SELECT {', '.join(cols)} FROM {_q(log.table)} ORDER BY 1, 3, 4").fetchall()
    changes: list[Change] = []
    for day, etype, eid, ctype, old, new in rows:
        key = str(eid)
        level = str(etype) if etype in env.labels else None
        if level is None or key not in env.labels[level]:
            level = next((lv for lv in env.levels() if key in env.labels[lv]), None)
        if level is None:
            continue  # the change names an entity with no data
        changes.append(
            Change(
                day=day if isinstance(day, date) else date.fromisoformat(str(day)[:10]),
                level=level,
                key=key,
                label=env.labels[level][key],
                change_type=str(ctype),
                old=None if old is None else str(old),
                new=None if new is None else str(new),
                material=_is_material(
                    str(ctype), old, new, env.model.tuning.material_budget_change
                ),
            )
        )
    return changes


def _is_material(change_type: str, old: object, new: object, threshold: float) -> bool:
    if change_type == "launched":
        return False
    if change_type == "budget_change":
        try:
            return abs(float(str(new)) / float(str(old)) - 1) >= threshold
        except (TypeError, ValueError, ZeroDivisionError):
            return True
    return True  # paused, audience_edit and any other change type


def _logged_cause(env: Env, con: duckdb.DuckDBPyConnection, change: Change) -> Cause | None:
    t = env.model.tuning
    model = env.model
    if change.change_type == "paused":
        parent = _parent(env, change.level, change.key)
        if parent is None:
            return None
        scope_level, scope_key = parent
    else:
        scope_level, scope_key = change.level, change.key
    d = change.day
    before0, before1 = d - timedelta(days=t.effect_before_days), d - timedelta(days=1)
    after_days = (
        t.effect_after_days_audience
        if change.change_type == "audience_edit"
        else t.effect_after_days
    )
    after0, after1 = d, d + timedelta(days=after_days - 1)
    before = _totals(model, con, scope_level, scope_key, before0, before1)
    after = _totals(model, con, scope_level, scope_key, after0, after1)
    if (
        before.conversions < t.effect_min_conversions_before
        or after.conversions < t.effect_min_conversions_after
        or before.cpl is None
        or after.cpl is None
    ):
        return None
    effect = _rel(after.cpl, before.cpl)
    if effect < t.effect_min_scope:
        return None

    evidence: Evidence = {
        "change_type": change.change_type,
        "old": change.old or "",
        "new": change.new or "",
        "cpl_before": round(before.cpl, 4),
        "cpl_after": round(after.cpl, 4),
        "conversions_before": before.conversions,
        "conversions_after": after.conversions,
        "effect": round(effect, 4),
        "spend_per_day_before": round(before.spend / t.effect_before_days, 4),
        "spend_per_day_after": round(after.spend / after_days, 4),
        "after_days": float(after_days),
    }
    parent = _parent(env, scope_level, scope_key)
    if parent is not None:
        p_before = _totals(model, con, parent[0], parent[1], before0, before1)
        p_after = _totals(model, con, parent[0], parent[1], after0, after1)
        if p_before.cpl is not None and p_after.cpl is not None:
            p_effect = _rel(p_after.cpl, p_before.cpl)
            if p_effect >= t.effect_min_parent:
                evidence.update(
                    {
                        "parent_level": parent[0],
                        "parent_key": parent[1],
                        "parent_cpl_before": round(p_before.cpl, 4),
                        "parent_cpl_after": round(p_after.cpl, 4),
                    }
                )
    spend_jump = _rel(after.spend / after_days, before.spend / t.effect_before_days)
    if spend_jump >= t.spend_jump:
        evidence["spend_jump"] = round(spend_jump, 4)
    if change.change_type == "paused":
        own = _totals(model, con, change.level, change.key, before0, before1)
        evidence["paused_share"] = round(own.spend / before.spend, 4) if before.spend else 0.0
    return Cause(
        cause_id=f"logged:{change.key}:{d.isoformat()}",
        kind="logged",
        entity_level=change.level,
        entity_key=change.key,
        entity_label=change.label,
        scope_level=scope_level,
        scope_key=scope_key,
        scope_label=env.labels[scope_level][scope_key],
        start=d,
        end=after1,
        evidence=evidence,
        logged=True,
        needs_review=False,
    )


# --- 2. tracking ----------------------------------------------------------------------


def _series(
    env: Env, con: duckdb.DuckDBPyConnection, level: str, period_expr: str
) -> dict[str, dict[date, tuple[float, float, float, float, float]]]:
    """key -> period -> (spend, conversions, clicks, impressions, reach)."""
    model = env.model
    rows = con.execute(
        f"SELECT {level_key(model, level)}, CAST({period_expr} AS DATE), {_sum(model, 'spend')}, "
        f"{_sum(model, 'conversions')}, {_sum(model, 'clicks')}, {_sum(model, 'impressions')}, "
        f"{_sum(model, 'reach')} {from_clause(model, [level], [])} GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    out: dict[str, dict[date, tuple[float, float, float, float, float]]] = {}
    for key, period, spend, conversions, clicks, impressions, reach in rows:
        out.setdefault(str(key), {})[period] = (
            float(spend),
            float(conversions),
            float(clicks),
            float(impressions),
            float(reach),
        )
    return out


def _tracking(env: Env, con: duckdb.DuckDBPyConnection) -> list[Cause]:
    t = env.model.tuning
    model, ctx = env.model, env.ctx
    top = env.levels()[0]
    if ctx.grain == "day":
        steps = [
            ctx.data_start + timedelta(days=i)
            for i in range((ctx.data_end - ctx.data_start).days + 1)
        ]
        baseline_n, step_days = t.tracking_baseline_days, 1
        series = _series(env, con, top, date_col(model))
    else:
        steps = week_starts(ctx)
        baseline_n, step_days = t.tracking_weekly_baseline_weeks, 7
        series = _series(env, con, top, date_col(model))
    causes: list[Cause] = []
    for key, _label in env.entities[top]:
        data = series.get(key, {})
        flagged: list[tuple[date, float, float, float, float]] = []
        for i in range(baseline_n, len(steps)):
            prev = [data.get(s, (0.0,) * 5) for s in steps[i - baseline_n : i]]
            base_conv = sum(p[1] for p in prev) / baseline_n
            base_clicks = sum(p[2] for p in prev) / baseline_n
            _, conv, clicks, _, _ = data.get(steps[i], (0.0,) * 5)
            if (
                base_conv >= t.tracking_min_baseline
                and conv <= t.tracking_max_share * base_conv
                and clicks >= t.tracking_min_click_share * base_clicks
            ):
                flagged.append((steps[i], conv, clicks, base_conv, base_clicks))
        runs: list[list[tuple[date, float, float, float, float]]] = []
        for item in flagged:
            if runs and (item[0] - runs[-1][-1][0]).days == step_days:
                runs[-1].append(item)
            else:
                runs.append([item])
        for run in runs:
            if len(run) < 2:
                continue
            start, end = run[0][0], run[-1][0] + timedelta(days=step_days - 1)
            label = env.labels[top][key]
            evidence: Evidence = {
                "days": float(len(run)),
                "conversions": sum(r[1] for r in run),
                "clicks_per_step": round(sum(r[2] for r in run) / len(run), 2),
                "baseline_conversions": round(run[0][3], 2),
                "baseline_clicks": round(run[0][4], 2),
                "dates": ",".join(r[0].isoformat() for r in run),
            }
            evidence.update(_tracking_child(env, con, key, start, baseline_n * step_days))
            causes.append(
                Cause(
                    cause_id=f"tracking:{key}:{start.isoformat()}",
                    kind="tracking",
                    entity_level=top,
                    entity_key=key,
                    entity_label=label,
                    scope_level=top,
                    scope_key=key,
                    scope_label=label,
                    start=start,
                    end=end,
                    evidence=evidence,
                    logged=False,
                    needs_review=True,
                )
            )
    return causes


def _tracking_child(
    env: Env, con: duckdb.DuckDBPyConnection, top_key: str, start: date, baseline_days: int
) -> Evidence:
    """The child of the top entity with the most baseline conversions per day, if it has at
    least tuning.tracking_child_min_baseline (so a question about it is meaningful)."""
    t = env.model.tuning
    names = env.levels()
    if len(names) < 2:
        return {}
    child_level = names[1]
    best: tuple[float, str] | None = None
    for key, _ in env.entities[child_level]:
        if env.parent_of[child_level].get(key, {}).get(names[0]) != top_key:
            continue
        totals = _totals(
            env.model,
            con,
            child_level,
            key,
            start - timedelta(days=baseline_days),
            start - timedelta(days=1),
        )
        per_day = totals.conversions / baseline_days
        if best is None or (per_day, key) > best:
            best = (per_day, key)
    if best is None or best[0] < t.tracking_child_min_baseline:
        return {}
    return {
        "child_level": child_level,
        "child_key": best[1],
        "child_baseline_conversions": round(best[0], 2),
    }


# --- 3. fatigue -----------------------------------------------------------------------


def _week_expr(env: Env) -> str:
    col = date_col(env.model)
    return f"date_trunc('week', {col})" if env.ctx.grain == "day" else col


def _fatigue(env: Env, con: duckdb.DuckDBPyConnection, material: list[Change]) -> list[Cause]:
    t = env.model.tuning
    model = env.model
    if "reach" not in model.fact.measures or "clicks" not in model.fact.measures:
        return []
    bottom = env.levels()[-1]
    weeks = week_starts(env.ctx)
    series = _series(env, con, bottom, _week_expr(env))
    causes: list[Cause] = []
    for key, label in env.entities[bottom]:
        data = series.get(key, {})
        for i in range(len(weeks) - t.fatigue_weeks + 1):
            window = weeks[i : i + t.fatigue_weeks]
            first = [data.get(w) for w in window[:2]]
            last = [data.get(w) for w in window[-2:]]
            if any(p is None for p in first + last):
                continue
            impr0 = sum(p[3] for p in first if p)
            reach0 = sum(p[4] for p in first if p)
            clicks0 = sum(p[2] for p in first if p)
            impr1 = sum(p[3] for p in last if p)
            reach1 = sum(p[4] for p in last if p)
            clicks1 = sum(p[2] for p in last if p)
            if not (impr0 and reach0 and impr1 and reach1 and clicks0):
                continue
            f0, f1 = impr0 / reach0, impr1 / reach1
            c0, c1 = clicks0 / impr0, clicks1 / impr1
            start, end = window[0], window[-1] + timedelta(days=6)
            if (
                f1 >= t.fatigue_min_frequency
                and f1 - f0 >= t.fatigue_min_frequency_rise
                and c1 <= t.fatigue_max_ctr_share * c0
                and not any(
                    _on_entity_or_ancestor(env, ch, bottom, key) and start <= ch.day <= end
                    for ch in material
                )
            ):
                causes.append(
                    Cause(
                        cause_id=f"fatigue:{key}:{start.isoformat()}",
                        kind="fatigue",
                        entity_level=bottom,
                        entity_key=key,
                        entity_label=label,
                        scope_level=bottom,
                        scope_key=key,
                        scope_label=label,
                        start=start,
                        end=end,
                        evidence={
                            "f0": round(f0, 4),
                            "f1": round(f1, 4),
                            "c0": round(c0, 6),
                            "c1": round(c1, 6),
                            "ctr_share": round(c1 / c0, 4),
                        },
                        logged=False,
                        needs_review=True,
                    )
                )
                break
    return causes


# --- 4. calendar ----------------------------------------------------------------------


def _second_level(env: Env) -> str:
    names = env.levels()
    return names[1] if len(names) > 1 else names[0]


def _calendar(
    env: Env, con: duckdb.DuckDBPyConnection, earlier: list[Cause], material: list[Change]
) -> list[Cause]:
    """Monthly totals that differ only because months have different lengths (daily data)."""
    t = env.model.tuning
    if env.ctx.grain != "day":
        return []  # weekly rows cannot be split into calendar days
    model = env.model
    level = _second_level(env)
    months = full_months(env.ctx)
    month_expr = f"date_trunc('month', {date_col(model)})"
    series = _series(env, con, level, month_expr)
    causes: list[Cause] = []
    for key, label in env.entities[level]:
        data = series.get(key, {})
        for m1, m2 in pairwise(months):
            if (m2.start - m1.end).days != 1:
                continue
            d1, d2 = (m1.end - m1.start).days + 1, (m2.end - m2.start).days + 1
            t1, t2 = data.get(m1.start, (0.0,) * 5)[0], data.get(m2.start, (0.0,) * 5)[0]
            if d1 == d2 or not t1 or not t2:
                continue
            a1, a2 = t1 / d1, t2 / d2
            if abs(t2 / t1 - 1) < t.calendar_min_total_change:
                continue
            if abs(a2 / a1 - 1) > t.calendar_max_daily_change:
                continue
            if any(
                related(env, ch.level, ch.key, level, key) and m1.start <= ch.day <= m2.end
                for ch in material
            ) or any(
                _touches(env, c, level, key) and _overlaps(c.start, c.end, m1.start, m2.end)
                for c in earlier
            ):
                continue
            causes.append(
                Cause(
                    cause_id=f"calendar:{key}:{m2.key}",
                    kind="calendar",
                    entity_level=level,
                    entity_key=key,
                    entity_label=label,
                    scope_level=level,
                    scope_key=key,
                    scope_label=label,
                    start=m1.start,
                    end=m2.end,
                    evidence={
                        "month1": m1.text,
                        "month2": m2.text,
                        "days1": float(d1),
                        "days2": float(d2),
                        "total1": round(t1, 2),
                        "total2": round(t2, 2),
                        "daily1": round(a1, 2),
                        "daily2": round(a2, 2),
                    },
                    logged=False,
                    needs_review=False,
                )
            )
    return causes


# --- 5. noise -------------------------------------------------------------------------


def _noise(
    env: Env, con: duckdb.DuckDBPyConnection, earlier: list[Cause], material: list[Change]
) -> list[Cause]:
    t = env.model.tuning
    if "clicks" not in env.model.fact.measures:
        return []
    level = _second_level(env)
    weeks = week_starts(env.ctx)
    series = _series(env, con, level, _week_expr(env))
    pad = timedelta(days=t.noise_exclusion_days)
    causes: list[Cause] = []
    for key, label in env.entities[level]:
        data = series.get(key, {})
        for w0, w1 in pairwise(weeks):
            p, c = data.get(w0), data.get(w1)
            if p is None or c is None:
                continue
            (s0, v0, k0, _, _), (s1, v1, k1, _, _) = p, c
            if v0 < t.noise_min_baseline or not s0 or not k0:
                continue
            jump = v1 / v0 - 1
            if abs(jump) < t.noise_min_jump:
                continue
            if (
                abs(s1 / s0 - 1) > t.noise_max_input_change
                or abs(k1 / k0 - 1) > t.noise_max_input_change
            ):
                continue
            lo, hi = w1 - pad, w1 + timedelta(days=6) + pad
            if any(
                related(env, ch.level, ch.key, level, key) and lo <= ch.day <= hi for ch in material
            ) or any(
                c2.kind in ("logged", "tracking", "fatigue")
                and _touches(env, c2, level, key)
                and _overlaps(c2.start, c2.end, lo, hi)
                for c2 in earlier
            ):
                continue
            causes.append(
                Cause(
                    cause_id=f"noise:{key}:{w1.isoformat()}",
                    kind="noise",
                    entity_level=level,
                    entity_key=key,
                    entity_label=label,
                    scope_level=level,
                    scope_key=key,
                    scope_label=label,
                    start=w1,
                    end=w1 + timedelta(days=6),
                    evidence={
                        "direction": "jump" if jump > 0 else "drop",
                        "conversions0": v0,
                        "conversions1": v1,
                        "s0": round(s0, 2),
                        "s1": round(s1, 2),
                        "k0": k0,
                        "k1": k1,
                        "r0": round(v0 / k0, 4),
                        "r1": round(v1 / k1, 4),
                    },
                    logged=False,
                    needs_review=True,
                )
            )
    return causes


def detect_causes(env: Env, con: duckdb.DuckDBPyConnection) -> Detection:
    """Run every detector, in order: logged, tracking, fatigue, calendar, noise."""
    changes = read_changes(env, con)
    material = [c for c in changes if c.material]
    detection = Detection(changes=changes)
    for change in material:
        cause = _logged_cause(env, con, change)
        if cause is not None:
            detection.causes.append(cause)
    if env.ctx.grain in ("day", "week"):
        detection.causes.extend(_tracking(env, con))
    detection.causes.extend(_fatigue(env, con, material))
    detection.causes.extend(_calendar(env, con, list(detection.causes), material))
    detection.causes.extend(_noise(env, con, list(detection.causes), material))
    return detection
