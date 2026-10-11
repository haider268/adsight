from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO = REPO_ROOT / "benchmark" / "convoprobe" / "marketers.yaml"


def test_marketers_scenario_loads_in_convoprobe() -> None:
    scenario_module = pytest.importorskip("convoprobe.scenario")  # the optional `probe` extra
    scenario = scenario_module.load_scenario(SCENARIO)
    assert scenario.name == "adsight-marketers" and scenario.max_turns == 5
    assert [p.id for p in scenario.personas] == [
        "follow_up",
        "changes_mind",
        "wants_roas",
        "why_drop",
        "vague",
        "tiktok",
    ]
    assert [r.id for r in scenario.rubric] == [
        "no_fabrication",
        "follows_context",
        "helpful_and_honest",
    ]


def test_the_adapter_loads_through_convoprobe() -> None:
    adapters = pytest.importorskip("convoprobe.adapters")
    adapter = adapters.load_adapter("adsight.probe_adapter:agent")
    assert type(adapter).__name__ == "CallableAdapter"
