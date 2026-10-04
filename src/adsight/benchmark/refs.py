"""Reference SQL: turn a reference query's result rows into a question's Expected answer."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import duckdb

from adsight.benchmark.questions import (
    EntityScoring,
    Expected,
    NumericScoring,
    NumericSetScoring,
    Question,
    Scoring,
    TrendScoring,
)

_DP = 6

Direction = Literal["up", "down", "flat"]


def _num(value: Any) -> float:
    return round(float(value), _DP)


def expected_from_rows(
    scoring: Scoring, columns: list[str], rows: Sequence[tuple[Any, ...]]
) -> Expected:
    if isinstance(scoring, NumericScoring):
        if len(rows) != 1:
            raise ValueError(f"numeric reference must return exactly one row, got {len(rows)}")
        return Expected(values={col: _num(v) for col, v in zip(columns, rows[0], strict=True)})

    if isinstance(scoring, EntityScoring):
        if not rows:
            raise ValueError("entity reference returned no rows")
        top = rows[0]
        if len(columns) >= 3 and len(rows) >= 2 and rows[1][2] == top[2]:
            raise ValueError("tie")
        return Expected(
            entity_id=str(top[0]),
            entity_name=str(top[1]) if len(columns) >= 2 else None,
        )

    if isinstance(scoring, NumericSetScoring):
        values: dict[str, float] = {}
        for label, value in rows:
            key = str(label)
            if key in values:
                raise ValueError(f"duplicate label {key!r}")
            values[key] = _num(value)
        return Expected(values=values)

    if isinstance(scoring, TrendScoring):
        if len(rows) < 2:
            raise ValueError(f"trend reference needs at least 2 rows, got {len(rows)}")
        first, last = _num(rows[0][1]), _num(rows[-1][1])
        change = (last - first) / first * 100
        direction: Direction
        if change > scoring.flat_band_pct:
            direction = "up"
        elif change < -scoring.flat_band_pct:
            direction = "down"
        else:
            direction = "flat"
        return Expected(values={"first": first, "last": last}, direction=direction)

    raise ValueError(f"{scoring.type} scoring has no reference SQL")


def compute_expected(question: Question, sql_dir: Path, con: duckdb.DuckDBPyConnection) -> Expected:
    """Run the question's reference SQL on a read-only connection and derive Expected."""
    if question.reference_sql is None:
        raise ValueError(f"{question.id} has no reference SQL")
    sql = (sql_dir / question.reference_sql).read_text(encoding="utf-8")
    cursor = con.execute(sql)
    columns = [d[0] for d in cursor.description or []]
    return expected_from_rows(question.scoring, columns, cursor.fetchall())
