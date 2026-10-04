import pytest

from adsight.data.generator import Dataset, generate


@pytest.fixture(scope="session")
def dataset7() -> Dataset:
    """The full dataset for seed 7, with the five injected incidents."""
    return generate(7)


@pytest.fixture(scope="session")
def baseline7() -> Dataset:
    """The clean baseline for seed 7: no incidents."""
    return generate(7, incidents=[])
