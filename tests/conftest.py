from pathlib import Path
from typing import Any

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
