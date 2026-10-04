from datetime import date

from adsight.data.catalog import ADS_BY_ID, LEARNING_RESET_BUDGET_CHANGE
from adsight.data.events import (
    ChangeEvent,
    ad_active_on,
    audience_on,
    baseline_events,
    budget_on,
    learning_phase,
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


def _event(
    day: date, entity_type: str, entity_id: str, change: str, old: str, new: str
) -> ChangeEvent:
    return ChangeEvent.model_validate(
        {
            "date": day,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "change_type": change,
            "old_value": old,
            "new_value": new,
        }
    )


def test_learning_phase_reports_cause() -> None:
    ad = ADS_BY_ID["AD025"]  # S09
    budget = _event(date(2026, 3, 1), "ad_set", "S09", "budget_change", "45.00", "60.00")
    edit = _event(
        date(2026, 5, 18), "ad_set", "S09", "audience_edit", "lookalike_1pct", "lookalike_3pct"
    )
    events = [*baseline_events(), budget, edit]
    assert learning_phase(ad, date(2026, 2, 1), events) == (date(2026, 1, 1), "launch")
    assert learning_phase(ad, date(2026, 3, 2), events) == (date(2026, 3, 1), "budget")
    assert learning_phase(ad, date(2026, 5, 20), events) == (date(2026, 5, 18), "audience")
    assert learning_start(ad, date(2026, 5, 20), events) == date(2026, 5, 18)


def test_ad_active_on_follows_pause_and_resume() -> None:
    ad = ADS_BY_ID["AD019"]
    pause = _event(date(2026, 3, 16), "ad", "AD019", "paused", "active", "paused")
    resume = _event(date(2026, 4, 1), "ad", "AD019", "resumed", "paused", "active")
    events = [*baseline_events(), pause, resume]
    assert ad_active_on(ad, date(2026, 3, 15), events)
    assert not ad_active_on(ad, date(2026, 3, 16), events)
    assert not ad_active_on(ad, date(2026, 3, 31), events)
    assert ad_active_on(ad, date(2026, 4, 1), events)
    assert ad_active_on(ADS_BY_ID["AD020"], date(2026, 3, 20), events)


def test_audience_on_applies_from_its_date() -> None:
    edit = _event(
        date(2026, 5, 18), "ad_set", "S09", "audience_edit", "lookalike_1pct", "lookalike_3pct"
    )
    events = [*baseline_events(), edit]
    assert audience_on("S09", date(2026, 5, 17), events) == "lookalike_1pct"
    assert audience_on("S09", date(2026, 5, 18), events) == "lookalike_3pct"
    assert audience_on("S10", date(2026, 6, 29), events) == "retargeting_30d"


def test_routine_decoys_stay_below_learning_reset() -> None:
    decoys = {e.entity_id: e for e in baseline_events() if e.change_type == "budget_change"}
    for ad_set_id in ("S08", "S14", "S10", "S16"):
        event = decoys[ad_set_id]
        assert event.old_value is not None and event.new_value is not None
        old, new = float(event.old_value), float(event.new_value)
        assert abs(new - old) / old < LEARNING_RESET_BUDGET_CHANGE
        assert not resets_learning(event)
