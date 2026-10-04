from pathlib import Path

import pytest

from adsight.benchmark.questions import (
    ClarifyScoring,
    EntityScoring,
    NumericScoring,
    NumericSetScoring,
    TrendScoring,
    load_questions,
)
from adsight.benchmark.refs import compute_expected, expected_from_rows
from adsight.data.build import open_readonly

# --- expected_from_rows ----------------------------------------------------


def test_numeric() -> None:
    expected = expected_from_rows(
        NumericScoring(unit="usd"), ["total_spend", "leads"], [(7037.2612345678, 12)]
    )
    assert expected.values == {"total_spend": 7037.261235, "leads": 12.0}


def test_numeric_two_rows_raises() -> None:
    with pytest.raises(ValueError, match="exactly one row"):
        expected_from_rows(NumericScoring(unit="usd"), ["x"], [(1,), (2,)])


def test_entity() -> None:
    expected = expected_from_rows(
        EntityScoring(),
        ["id", "name", "cpl"],
        [("C5", "Laser Hair Removal", 9.1), ("C4", "B", 9.5)],
    )
    assert expected.entity_id == "C5"
    assert expected.entity_name == "Laser Hair Removal"
    only_id = expected_from_rows(EntityScoring(), ["id"], [("C1",)])
    assert only_id.entity_id == "C1"
    assert only_id.entity_name is None


def test_entity_tie_raises() -> None:
    with pytest.raises(ValueError, match="tie"):
        expected_from_rows(
            EntityScoring(), ["id", "name", "cpl"], [("C5", "a", 9.1), ("C4", "b", 9.1)]
        )


def test_entity_no_rows_raises() -> None:
    with pytest.raises(ValueError, match="no rows"):
        expected_from_rows(EntityScoring(), ["id"], [])


def test_numeric_set() -> None:
    expected = expected_from_rows(
        NumericSetScoring(unit="ratio"),
        ["label", "ctr"],
        [("A1 image", 0.0178), ("A1 video", 0.0192)],
    )
    assert expected.values == {"A1 image": 0.0178, "A1 video": 0.0192}


def test_numeric_set_duplicate_label_raises() -> None:
    with pytest.raises(ValueError, match="duplicate label"):
        expected_from_rows(NumericSetScoring(unit="ratio"), ["l", "v"], [("a", 1), ("a", 2)])


@pytest.mark.parametrize(
    ("last", "direction"), [(104.0, "flat"), (106.0, "up"), (94.0, "down"), (96.0, "flat")]
)
def test_trend_flat_band(last: float, direction: str) -> None:
    expected = expected_from_rows(
        TrendScoring(unit="usd"), ["week", "cpl"], [("w1", 100.0), ("w2", 250.0), ("w3", last)]
    )
    assert expected.direction == direction
    assert expected.values == {"first": 100.0, "last": last}


def test_trend_needs_two_rows() -> None:
    with pytest.raises(ValueError, match="at least 2 rows"):
        expected_from_rows(TrendScoring(unit="usd"), ["w", "v"], [("w1", 1.0)])


def test_other_scoring_types_raise() -> None:
    with pytest.raises(ValueError, match="no reference SQL"):
        expected_from_rows(ClarifyScoring(), ["x"], [(1,)])


# --- reference SQL against the seed-7 database -----------------------------


def test_stored_refs_are_current(bench_data_dir: Path, questions_path: Path, sql_dir: Path) -> None:
    con = open_readonly(bench_data_dir / "adsight.duckdb")
    try:
        sql_questions = [q for q in load_questions(questions_path) if q.reference_sql]
        assert len(sql_questions) == 4
        for q in sql_questions:
            assert q.expected is not None, q.id
            assert compute_expected(q, sql_dir, con) == q.expected, q.id
    finally:
        con.close()


def test_sample_values(questions_path: Path) -> None:
    by_id = {q.id: q for q in load_questions(questions_path)}
    lk01, ag01, cp01, tr01 = (by_id[i].expected for i in ("LK01", "AG01", "CP01", "TR01"))
    assert lk01 is not None and ag01 is not None and cp01 is not None and tr01 is not None
    assert lk01.values == {"total_spend": 7037.26}
    assert ag01.entity_id == "C5"
    assert set(cp01.values) == {
        "A1 image",
        "A1 video",
        "A2 image",
        "A2 video",
        "A3 image",
        "A3 video",
    }
    assert tr01.direction == "down"
    assert tr01.values == {"first": 15.80, "last": 14.51}
