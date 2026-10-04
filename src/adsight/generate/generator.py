"""Seeded benchmark generation: enumerate, lint, select, and write the artefacts.

Same seed and same data give byte-identical output.
"""

import json
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from adsight.benchmark.questions import PREFIX_BY_CATEGORY, Question, save_questions
from adsight.benchmark.refs import compute_expected
from adsight.generate.context import build_context, write_context
from adsight.generate.lint import lint
from adsight.generate.templates import (
    TEMPLATES,
    Candidate,
    Template,
    ambiguous_candidate,
    ambiguous_phrasings,
    available_probes,
    build_env,
    unanswerable_candidate,
)
from adsight.semantic.model import SemanticModel

TARGETS: dict[str, int] = {
    "lookup": 15,
    "aggregation": 15,
    "comparison": 15,
    "trend": 15,
    "root_cause": 0,
    "ambiguous": 10,
    "unanswerable": 10,
}
OUTPUT_ORDER = ("lookup", "aggregation", "comparison", "trend", "ambiguous", "unanswerable")
MAX_LINTED_PER_TEMPLATE = 60
MAX_TEMPLATE_SHARE = 0.40
TREND_PER_DIRECTION = 5
TREND_FLAT_BAND_PCT = 5.0
MAX_EXAMPLES_PER_RULE = 3
DIRECTIONS = ("up", "down", "flat")


@dataclass
class GenerateReport:
    candidates_evaluated: int = 0
    accepted: int = 0
    rejected_by_rule: dict[str, int] = field(default_factory=dict)
    per_category: dict[str, dict[str, int]] = field(default_factory=dict)
    trend_directions: dict[str, int] = field(default_factory=dict)
    shortfalls: list[str] = field(default_factory=list)
    rejected_examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
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


def _trend_direction(con: duckdb.DuckDBPyConnection, cand: Candidate) -> str:
    if cand.sql is None:
        raise ValueError(f"trend candidate {cand.params} has no SQL")
    rows = con.execute(cand.sql).fetchall()
    first, last = float(rows[0][1]), float(rows[-1][1])
    change = (last - first) / first * 100
    if change > TREND_FLAT_BAND_PCT:
        return "up"
    if change < -TREND_FLAT_BAND_PCT:
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
        slots = template.slots(env)
        random.Random(f"{seed}:{template.id}").shuffle(slots)
        accepted: list[Candidate] = []
        for params in slots[:MAX_LINTED_PER_TEMPLATE]:
            cand = template.build(env, params)
            if linter.check(cand, ctx):
                accepted.append(cand)
                if template.category == "trend":
                    directions[id(cand)] = _trend_direction(con, cand)
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
        _round_robin(pools, order, target, chosen, lambda c, chosen: True)
        selected[category] = chosen

    # 3. ambiguous and unanswerable: seeded picks from what this dataset allows
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

    # 4. write the artefacts
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
