"""SQL fragments generated from a semantic model: FROM/JOIN chains and column references.

The fact table is aliased `f`; a level stored in its own table is aliased by its level name.
"""

import re

from adsight.semantic.model import Level, MeasureName, SemanticModel

FACT_ALIAS = "f"

_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote(identifier: str) -> str:
    """Leave plain names readable; quote anything else."""
    if _PLAIN.match(identifier):
        return identifier
    return '"' + identifier.replace('"', '""') + '"'


def _alias(model: SemanticModel, level: Level) -> str:
    return FACT_ALIAS if model.in_fact(level) else _quote(level.name)


def _levels_to_join(model: SemanticModel, targets: set[str]) -> list[Level]:
    """Own-table levels needed to reach `targets`, ordered bottom-up (join order)."""
    if not targets:
        return []
    names = [lv.name for lv in model.levels]
    top_needed = min(names.index(t) for t in targets)
    chain = model.levels[top_needed:]  # every level from the highest target down to the bottom
    return [lv for lv in reversed(chain) if not model.in_fact(lv)]


def _join_condition(model: SemanticModel, level: Level) -> str:
    """How `level` joins: to the parent_key of the level below it, or to the fact row."""
    index = model.levels.index(level)
    below = model.levels[index + 1] if index + 1 < len(model.levels) else None
    if below is not None and not model.in_fact(below) and below.parent_key is not None:
        source = f"{_quote(below.name)}.{_quote(below.parent_key)}"
    else:
        source = f"{FACT_ALIAS}.{_quote(level.key)}"
    return f"{_quote(level.name)}.{_quote(level.key)} = {source}"


def from_clause(model: SemanticModel, levels: list[str], attributes: list[str]) -> str:
    """FROM the fact table plus the LEFT JOINs needed for these levels and attributes."""
    targets = {model.level(name).name for name in levels}
    for attr_name in attributes:
        attr = model.attribute(attr_name)
        if attr.table != model.fact.table:
            owner = next((lv for lv in model.levels if lv.table == attr.table), None)
            if owner is None:
                raise ValueError(f"attribute {attr_name!r} lives in a table with no level")
            targets.add(owner.name)
    parts = [f"FROM {_quote(model.fact.table)} {FACT_ALIAS}"]
    for lv in _levels_to_join(model, targets):
        parts.append(
            f"LEFT JOIN {_quote(lv.table)} {_quote(lv.name)} ON {_join_condition(model, lv)}"
        )
    return "\n".join(parts)


def level_key(model: SemanticModel, level: str) -> str:
    lv = model.level(level)
    return f"{_alias(model, lv)}.{_quote(lv.key)}"


def level_label(model: SemanticModel, level: str) -> str:
    lv = model.level(level)
    return f"{_alias(model, lv)}.{_quote(lv.label or lv.key)}"


def attribute_col(model: SemanticModel, name: str) -> str:
    attr = model.attribute(name)
    if attr.table == model.fact.table:
        return f"{FACT_ALIAS}.{_quote(attr.column)}"
    owner = next(lv for lv in model.levels if lv.table == attr.table)
    return f"{_quote(owner.name)}.{_quote(attr.column)}"


def measure_col(model: SemanticModel, measure: MeasureName) -> str:
    return f"{FACT_ALIAS}.{_quote(model.fact.measures[measure])}"


def date_col(model: SemanticModel) -> str:
    return f"{FACT_ALIAS}.{_quote(model.fact.date)}"
