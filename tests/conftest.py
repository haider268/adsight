import random
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from adsight.data.build import build_database
from adsight.data.generator import Dataset, generate
from adsight.data.manifest import build_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = REPO_ROOT / "benchmark" / "questions.jsonl"
SQL_DIR = REPO_ROOT / "benchmark" / "reference_sql"


@pytest.fixture(scope="session")
def questions_path() -> Path:
    """The committed benchmark questions file."""
    return QUESTIONS_PATH


@pytest.fixture(scope="session")
def sql_dir() -> Path:
    """The committed reference SQL folder."""
    return SQL_DIR


@pytest.fixture(scope="session")
def dataset7() -> Dataset:
    """The full dataset for seed 7, with the five injected incidents."""
    return generate(7)


@pytest.fixture(scope="session")
def baseline7() -> Dataset:
    """The clean baseline for seed 7: no incidents."""
    return generate(7, incidents=[])


@pytest.fixture(scope="session")
def bench_data_dir(tmp_path_factory: pytest.TempPathFactory, dataset7: Dataset) -> Path:
    """A data dir holding the seed-7 benchmark database, built once per session."""
    data_dir = tmp_path_factory.mktemp("bench-data")
    build_database(dataset7, data_dir / "adsight.duckdb")
    return data_dir


@pytest.fixture(scope="session")
def bench_manifest(dataset7: Dataset) -> dict[str, Any]:
    return build_manifest(dataset7, 7)


# --- a flat weekly database: no change log, no reach, levels in the fact table ------


def flat_rows() -> list[tuple[object, ...]]:
    rng = random.Random(1)
    rows: list[tuple[object, ...]] = []
    for c, name in enumerate(("Alpha", "Beta", "Gamma"), start=1):
        for g in (1, 2):
            for w in range(30):
                impr = rng.randint(15000, 30000)
                clicks = round(impr * rng.uniform(0.01, 0.02))
                conversions = round(clicks * rng.uniform(0.03, 0.08))
                rows.append(
                    (
                        date(2026, 1, 5) + timedelta(days=7 * w),
                        f"C{c}",
                        name,
                        f"C{c}-G{g}",
                        rng.uniform(200, 400),
                        impr,
                        clicks,
                        conversions,
                    )
                )
    return rows


def make_flat(path: Path, extra_revenue: bool = False) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE perf(week_start DATE, campaign_id VARCHAR, campaign_name VARCHAR, "
            "ad_group VARCHAR, cost DOUBLE, impr BIGINT, clicks BIGINT, conversions BIGINT)"
        )
        con.executemany("INSERT INTO perf VALUES (?, ?, ?, ?, ?, ?, ?, ?)", flat_rows())
        if extra_revenue:
            con.execute("ALTER TABLE perf ADD COLUMN revenue DOUBLE")
    finally:
        con.close()


@pytest.fixture(scope="session")
def make_flat_db() -> Callable[..., None]:
    """Build the flat weekly fixture database at a path (optionally with a revenue column)."""
    return make_flat
