import pytest

from adsight.data.generator import Dataset, generate


@pytest.fixture(scope="session")
def dataset7() -> Dataset:
    """The baseline dataset for seed 7, generated once per test session."""
    return generate(7)
