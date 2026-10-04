from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from adsight.data.build import build_database, open_readonly
from adsight.data.generator import Dataset

EXPECTED = {
    "accounts": 3,
    "campaigns": 9,
    "ad_sets": 18,
    "ads": 54,
    "daily_ad_metrics": 8640,
    "events_log": 15,
}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory, dataset7: Dataset) -> Path:
    path = tmp_path_factory.mktemp("db") / "nested" / "adsight.duckdb"
    assert build_database(dataset7, path) == EXPECTED
    return path


def test_rebuild_gives_same_counts(built: Path, dataset7: Dataset) -> None:
    assert build_database(dataset7, built) == EXPECTED


def test_readonly_rejects_insert(built: Path) -> None:
    con = open_readonly(built)
    try:
        with pytest.raises(duckdb.Error):
            con.execute("INSERT INTO accounts VALUES ('A9', 'x', 'y')")
    finally:
        con.close()


def test_spend_round_trips_to_the_cent(built: Path, dataset7: Dataset) -> None:
    con = open_readonly(built)
    try:
        row = con.execute("SELECT SUM(spend) FROM daily_ad_metrics").fetchone()
    finally:
        con.close()
    assert row is not None
    expected = sum(Decimal(str(m.spend)) for m in dataset7.metrics)
    assert Decimal(row[0]) == expected


def test_event_ids_and_end_budget(built: Path) -> None:
    con = open_readonly(built)
    try:
        ids = [r[0] for r in con.execute("SELECT event_id FROM events_log ORDER BY 1").fetchall()]
        budget = con.execute("SELECT daily_budget FROM ad_sets WHERE ad_set_id = 'S02'").fetchone()
    finally:
        con.close()
    assert ids == list(range(1, 16))
    assert budget is not None
    assert budget[0] == Decimal("44.00")
