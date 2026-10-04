from datetime import date

from adsight.data.catalog import ADS_BY_ID
from adsight.data.events import (
    ChangeEvent,
    baseline_events,
    budget_on,
    learning_start,
    resets_learning,
)
from adsight.data.generator import Dataset


def test_baseline_has_15_events_sorted() -> None:
    events = baseline_events()
    assert len(events) == 15
    assert sum(e.change_type == "launched" for e in events) == 9
    assert sum(e.change_type == "budget_change" for e in events) == 6
    assert events == sorted(events, key=lambda e: (e.date, e.entity_id))
    launch = next(e for e in events if e.entity_id == "C9")
    assert launch.entity_type == "campaign"
    assert launch.date == date(2026, 4, 1)
    assert launch.old_value is None
    assert launch.new_value is None


def test_budget_change_applies_on_its_date() -> None:
    events = baseline_events()
    assert budget_on("S02", date(2026, 2, 9), events) == 40.0
    assert budget_on("S02", date(2026, 2, 10), events) == 44.0
    assert budget_on("S01", date(2026, 6, 29), events) == 60.0


def test_end_of_window_budget_in_output(dataset7: Dataset) -> None:
    budgets = {s.ad_set_id: s.daily_budget for s in dataset7.ad_sets}
    assert budgets["S02"] == 44.0
    assert budgets["S04"] == 18.0
    assert budgets["S01"] == 60.0


def test_no_routine_change_resets_learning() -> None:
    events = baseline_events()
    assert not any(resets_learning(e) for e in events)
    for ad in ADS_BY_ID.values():
        assert learning_start(ad, date(2026, 6, 29), events) == ad.launched_date


def test_large_budget_change_and_audience_edit_reset_learning() -> None:
    ad = ADS_BY_ID["AD001"]
    big = ChangeEvent(
        date=date(2026, 3, 1),
        entity_type="ad_set",
        entity_id="S01",
        change_type="budget_change",
        old_value="60.00",
        new_value="72.00",
    )
    edit = ChangeEvent(
        date=date(2026, 4, 1),
        entity_type="ad_set",
        entity_id="S01",
        change_type="audience_edit",
        old_value="broad_45_65",
        new_value="broad_35_65",
    )
    events = [*baseline_events(), big, edit]
    assert learning_start(ad, date(2026, 2, 28), events) == date(2026, 1, 1)
    assert learning_start(ad, date(2026, 3, 1), events) == date(2026, 3, 1)
    assert learning_start(ad, date(2026, 5, 1), events) == date(2026, 4, 1)
