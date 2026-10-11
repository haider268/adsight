"""Seeded benchmark generation: enumerate, lint, select, and write the artefacts.

Same seed and same data give byte-identical output.
"""

import json
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import duckdb

from adsight.benchmark.questions import (
    PREFIX_BY_CATEGORY,
    Question,
    load_questions,
    save_questions,
)
from adsight.benchmark.refs import compute_expected
from adsight.generate.causes import Cause, detect_causes
from adsight.generate.context import build_context, write_context
from adsight.generate.lint import lint
from adsight.generate.templates import (
    TEMPLATES,
    Candidate,
    Env,
    Params,
    Template,
    ambiguous_candidate,
    ambiguous_phrasings,
    available_probes,
    build_env,
    root_cause_candidates,
    unanswerable_candidate,
)
from adsight.semantic.model import SemanticModel

TARGETS: dict[str, int] = {
    "lookup": 15,
    "aggregation": 15,
    "comparison": 15,
    "trend": 15,
    "root_cause": 20,
    "ambiguous": 10,
    "unanswerable": 10,
}
OUTPUT_ORDER = (
    "lookup",
    "aggregation",
    "comparison",
    "trend",
    "root_cause",
    "ambiguous",
    "unanswerable",
)
MAX_LINTED_PER_TEMPLATE = 60
MAX_TEMPLATE_SHARE = 0.40
TREND_PER_DIRECTION = 5
MAX_EXAMPLES_PER_RULE = 3
DIRECTIONS = ("up", "down", "flat")
PERIOD_KIND_ORDER = ("month", "quarter", "last30", "whole", "week")
MAX_FATIGUE_QUESTIONS = 3
MAX_CALENDAR_FIRST_PASS = 2
MAX_NOISE_FIRST_PASS = 4


@dataclass
class GenerateReport:
    candidates_evaluated: int = 0
    accepted: int = 0
    rejected_by_rule: dict[str, int] = field(default_factory=dict)
    per_category: dict[str, dict[str, int]] = field(default_factory=dict)
    trend_directions: dict[str, int] = field(default_factory=dict)
    shortfalls: list[str] = field(default_factory=list)
    rejected_examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    root_cause: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "candidates_evaluated": self.candidates_evaluated,
            "accepted": self.accepted,
            "rejected_by_rule": self.rejected_by_rule,
            "per_category": self.per_category,
            "trend_directions": self.trend_directions,
            "shortfalls": self.shortfalls,
            "rejected_examples": self.rejected_examples,
        }


def _write_json(data: dict[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def _trend_direction(con: duckdb.DuckDBPyConnection, cand: Candidate, band: float) -> str:
    if cand.sql is None:
        raise ValueError(f"trend candidate {cand.params} has no SQL")
    rows = con.execute(cand.sql).fetchall()
    first, last = float(rows[0][1]), float(rows[-1][1])
    change = (last - first) / first * 100
    if change > band:
        return "up"
    if change < -band:
        return "down"
    return "flat"


class _Linter:
    """Lints candidates, remembering accepted keys (R8) and filling the report."""

    def __init__(
        self, model: SemanticModel, con: duckdb.DuckDBPyConnection, report: GenerateReport
    ):
        self.model, self.con, self.report = model, con, report
        self.seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    def check(self, cand: Candidate, ctx: Any) -> bool:
        self.report.candidates_evaluated += 1
        result = lint(cand, self.con, self.model, ctx, seen=self.seen)
        if result.ok:
            self.report.accepted += 1
            self.seen.add((cand.template, tuple(sorted(cand.params.items()))))
            return True
        rule = result.rule or "UNKNOWN"
        self.report.rejected_by_rule[rule] = self.report.rejected_by_rule.get(rule, 0) + 1
        examples = self.report.rejected_examples.setdefault(rule, [])
        if len(examples) < MAX_EXAMPLES_PER_RULE:
            examples.append(
                {
                    "template": cand.template,
                    "params": cand.params,
                    "question": cand.question,
                    "reason": result.reason,
                }
            )
        return False


def _round_robin(
    pools: dict[str, list[Candidate]],
    order: list[str],
    target: int,
    chosen: list[Candidate],
    wants: Callable[[Candidate, list[Candidate]], bool],
) -> None:
    """Take candidates in turn from each template's pool until `target` are chosen.

    No template supplies more than MAX_TEMPLATE_SHARE of the target (rounded up) unless
    the others ran out. `pools` and `chosen` are updated in place.
    """
    cap = math.ceil(target * MAX_TEMPLATE_SHARE)
    for capped in (True, False):
        progress = True
        while len(chosen) < target and progress:
            progress = False
            for template in order:
                if len(chosen) >= target:
                    break
                used = sum(1 for c in chosen if c.template == template)
                if capped and used >= cap:
                    continue
                pool = pools[template]
                for i, cand in enumerate(pool):
                    if wants(cand, chosen):
                        chosen.append(pool.pop(i))
                        progress = True
                        break


def _order_slots(slots: list[Params], env: Env, rng: random.Random) -> list[Params]:
    """Shuffle slots; slots with a period are shuffled within each period kind and then
    interleaved round-robin by kind, so the many weekly periods cannot crowd out the rest."""
    if not slots or "period" not in slots[0]:
        rng.shuffle(slots)
        return slots
    groups: dict[str, list[Params]] = {kind: [] for kind in PERIOD_KIND_ORDER}
    for params in slots:
        groups[env.periods[params["period"]].kind].append(params)
    for kind in PERIOD_KIND_ORDER:
        rng.shuffle(groups[kind])
    ordered: list[Params] = []
    for i in range(max(len(g) for g in groups.values())):
        for kind in PERIOD_KIND_ORDER:
            if i < len(groups[kind]):
                ordered.append(groups[kind][i])
    return ordered


def _is_week(env: Env, cand: Candidate) -> bool:
    return "period" in cand.params and env.periods[cand.params["period"]].kind == "week"


def _week_share_ok(env: Env, cand: Candidate, chosen: list[Candidate]) -> bool:
    """A weekly-period question may join only if weeks stay within max_week_share (rounded
    down) of the category's period-based questions, counting this one."""
    if not _is_week(env, cand):
        return True
    period_based = sum(1 for c in chosen if "period" in c.params) + 1
    weeks = sum(1 for c in chosen if _is_week(env, c)) + 1
    return weeks <= math.floor(env.model.tuning.max_week_share * period_based)


def select_root_causes(
    candidates: list[Candidate], causes: list[Cause], seed: int, target: int
) -> list[Candidate]:
    """Pick root-cause questions in order of preference, at most one per (cause, angle):
    logged causes first, then tracking, fatigue (capped), calendar and noise (capped), then
    leftovers round-robin by kind with only the fatigue cap still applying."""
    by_cause: dict[str, dict[str, Candidate]] = {}
    for cand in candidates:
        by_cause.setdefault(cand.params["cause_id"], {})[cand.params["angle"]] = cand
    kinds: dict[str, list[str]] = {}
    for cause in causes:
        if cause.cause_id in by_cause:
            kinds.setdefault(cause.kind, []).append(cause.cause_id)
    for kind, ids in kinds.items():
        random.Random(f"{seed}:RC:{kind}").shuffle(ids)

    chosen: list[Candidate] = []
    taken: set[tuple[str, str]] = set()

    def take(cause_id: str, angle: str) -> None:
        cand = by_cause.get(cause_id, {}).get(angle)
        if cand is not None and (cause_id, angle) not in taken and len(chosen) < target:
            chosen.append(cand)
            taken.add((cause_id, angle))

    for angle in ("RC_SCOPE", "RC_DECOY", "RC_PARENT", "RC_SPEND"):
        for cause_id in kinds.get("logged", []):
            take(cause_id, angle)
    for angle in ("RC_TRACK", "RC_TRACK_CHILD", "RC_TRACK_DECOY"):
        for cause_id in kinds.get("tracking", []):
            take(cause_id, angle)
    for cause_id in kinds.get("fatigue", [])[:MAX_FATIGUE_QUESTIONS]:
        take(cause_id, "RC_FATIGUE")
    for cause_id in kinds.get("calendar", [])[:MAX_CALENDAR_FIRST_PASS]:
        take(cause_id, "RC_CALENDAR")
    for cause_id in kinds.get("noise", [])[:MAX_NOISE_FIRST_PASS]:
        take(cause_id, "RC_NOISE")

    leftovers = {
        kind: [
            (cause_id, angle)
            for cause_id in ids
            for angle in by_cause[cause_id]
            if (cause_id, angle) not in taken
        ]
        for kind, ids in kinds.items()
    }
    progress = True
    while len(chosen) < target and progress:
        progress = False
        for kind in ("logged", "tracking", "fatigue", "calendar", "noise"):
            pool = leftovers.get(kind, [])
            fatigue_used = sum(1 for c in chosen if c.params["kind"] == "fatigue")
            if not pool or (kind == "fatigue" and fatigue_used >= MAX_FATIGUE_QUESTIONS):
                continue
            take(*pool.pop(0))
            progress = True
    return chosen


ReviewKey = tuple[str | None, str, str]


def _review_key(template: str | None, params: dict[str, str], question: str) -> ReviewKey:
    return template, json.dumps(params, sort_keys=True), question


def prior_reviews(path: Path) -> dict[ReviewKey, str]:
    """Human decisions (approved / rejected) already recorded in a questions.jsonl, keyed by
    template, params and question text. A missing file has none."""
    if not path.is_file():
        return {}
    return {
        _review_key(q.template, q.params, q.question): q.review
        for q in load_questions(path)
        if q.review in ("approved", "rejected")
    }


def _carry_reviews(cands: list[Candidate], prior: dict[ReviewKey, str]) -> list[Candidate]:
    """A pending candidate identical to an already decided question keeps that decision."""
    return [
        replace(c, review=prior.get(_review_key(c.template, c.params, c.question), c.review))
        if c.review == "pending"
        else c
        for c in cands
    ]


def _enough(category: str, accepted: list[Candidate], directions: dict[int, str]) -> bool:
    if category != "trend":
        return len(accepted) >= TARGETS[category]
    counts = {d: sum(1 for c in accepted if directions[id(c)] == d) for d in DIRECTIONS}
    return all(n >= TREND_PER_DIRECTION for n in counts.values())


def generate_benchmark(
    model: SemanticModel, con: duckdb.DuckDBPyConnection, seed: int, out_dir: Path
) -> GenerateReport:
    ctx = build_context(model, con)
    env = build_env(model, con, ctx)
    report = GenerateReport()
    linter = _Linter(model, con, report)
    directions: dict[int, str] = {}  # id(candidate) -> trend direction

    # 1. enumerate, shuffle and lint each template's candidates
    pools: dict[str, list[Candidate]] = {}
    by_category: dict[str, list[Template]] = {}
    for template in TEMPLATES:
        by_category.setdefault(template.category, []).append(template)
        slots = _order_slots(template.slots(env), env, random.Random(f"{seed}:{template.id}"))
        accepted: list[Candidate] = []
        for params in slots[:MAX_LINTED_PER_TEMPLATE]:
            cand = template.build(env, params)
            if linter.check(cand, ctx):
                accepted.append(cand)
                if template.category == "trend":
                    directions[id(cand)] = _trend_direction(
                        con, cand, model.tuning.trend_flat_band_pct
                    )
                if _enough(template.category, accepted, directions):
                    break
        pools[template.id] = accepted

    # 2. select per category, round-robin across templates
    selected: dict[str, list[Candidate]] = {}
    for category, templates in by_category.items():
        order = [t.id for t in templates]
        target = TARGETS[category]
        chosen: list[Candidate] = []
        if category == "trend":

            def in_quota(cand: Candidate, chosen: list[Candidate]) -> bool:
                d = directions[id(cand)]
                return sum(1 for c in chosen if directions[id(c)] == d) < TREND_PER_DIRECTION

            _round_robin(pools, order, target, chosen, in_quota)
            for d in DIRECTIONS:
                have = sum(1 for c in chosen if directions[id(c)] == d)
                if have < TREND_PER_DIRECTION:
                    report.shortfalls.append(
                        f"trend: only {have} {d} of {TREND_PER_DIRECTION} wanted; "
                        "filled from other directions"
                    )
        else:
            _round_robin(
                pools, order, target, chosen, lambda c, chosen: _week_share_ok(env, c, chosen)
            )
            if len(chosen) < target:
                report.shortfalls.append(
                    f"{category}: the {model.tuning.max_week_share:.0%} week-period cap left "
                    f"{len(chosen)} of {target}; filled with week-period questions"
                )
        _round_robin(pools, order, target, chosen, lambda c, chosen: True)
        selected[category] = chosen

    # 3. root cause: questions built from detected causes, never from raw jumps
    detection = detect_causes(env, con)
    rc_candidates = [
        c
        for c in root_cause_candidates(
            env, detection.causes, detection.decoys, model.tuning.decoy_window_days
        )
        if linter.check(c, ctx)
    ]
    selected["root_cause"] = _carry_reviews(
        select_root_causes(rc_candidates, detection.causes, seed, TARGETS["root_cause"]),
        prior_reviews(out_dir / "questions.jsonl"),
    )
    rc = selected["root_cause"]
    report.root_cause = {
        "causes_detected": {
            kind: sum(1 for c in detection.causes if c.kind == kind)
            for kind in ("logged", "tracking", "fatigue", "calendar", "noise")
        },
        "by_kind": {
            kind: sum(1 for c in rc if c.params["kind"] == kind)
            for kind in ("logged", "tracking", "fatigue", "calendar", "noise")
        },
        "by_angle": {
            angle: sum(1 for c in rc if c.template == angle)
            for angle in sorted({c.template for c in rc_candidates})
        },
        "pending_review": sum(1 for c in rc if c.review == "pending"),
        "approved": sum(1 for c in rc if c.review == "approved"),
        "rejected": sum(1 for c in rc if c.review == "rejected"),
    }

    # 4. ambiguous and unanswerable: seeded picks from what this dataset allows
    phrasings = ambiguous_phrasings(env)
    picked = random.Random(f"{seed}:AMBIGUOUS").sample(
        phrasings, min(TARGETS["ambiguous"], len(phrasings))
    )
    selected["ambiguous"] = [
        c for c in (ambiguous_candidate(k, t) for k, t in picked) if linter.check(c, ctx)
    ]
    probes = available_probes(env)
    picked_probes = random.Random(f"{seed}:UNANSWERABLE").sample(
        probes, min(TARGETS["unanswerable"], len(probes))
    )
    selected["unanswerable"] = [
        c for c in (unanswerable_candidate(env, p) for p in picked_probes) if linter.check(c, ctx)
    ]

    for category in TARGETS:
        got = len(selected.get(category, []))
        report.per_category[category] = {"target": TARGETS[category], "selected": got}
        if got < TARGETS[category]:
            report.shortfalls.append(f"{category}: {got} of {TARGETS[category]} selected")
    report.trend_directions = {
        d: sum(1 for c in selected.get("trend", []) if directions[id(c)] == d) for d in DIRECTIONS
    }

    # 5. write the artefacts
    out_dir.mkdir(parents=True, exist_ok=True)
    sql_dir = out_dir / "reference_sql"
    sql_dir.mkdir(exist_ok=True)
    for stale in sorted(sql_dir.glob("*.sql")):
        stale.unlink()
    questions: list[Question] = []
    for category in OUTPUT_ORDER:
        for i, cand in enumerate(selected.get(category, []), start=1):
            qid = f"{PREFIX_BY_CATEGORY[category]}{i:02d}"
            sql_file = None
            if cand.sql is not None:
                sql_file = f"{qid}.sql"
                (sql_dir / sql_file).write_text(cand.sql, encoding="utf-8", newline="\n")
            question = Question.model_validate(
                {
                    "id": qid,
                    "category": category,
                    "question": cand.question,
                    "scoring": cand.scoring,
                    "reference_sql": sql_file,
                    "notes": cand.notes,
                    "template": cand.template,
                    "params": cand.params,
                    "review": cand.review,
                }
            )
            if sql_file is not None:
                expected = compute_expected(question, sql_dir, con)
                question = question.model_copy(update={"expected": expected})
            questions.append(question)
    save_questions(questions, out_dir / "questions.jsonl")
    write_context(ctx, out_dir / "context.json")
    _write_json(report.to_json(), out_dir / "lint_report.json")
    return report
