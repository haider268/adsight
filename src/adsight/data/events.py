"""Change events, the baseline event history, and the state derived from events."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict

from adsight.data.catalog import (
    AD_SETS_BY_ID,
    CAMPAIGNS,
    LEARNING_RESET_BUDGET_CHANGE,
    Ad,
)

EntityType = Literal["account", "campaign", "ad_set", "ad"]
ChangeType = Literal[
    "launched", "budget_change", "paused", "resumed", "audience_edit", "creative_added"
]
LearningCause = Literal["launch", "budget", "audience"]


class ChangeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: dt.date
    entity_type: EntityType
    entity_id: str
    change_type: ChangeType
    old_value: str | None
    new_value: str | None


_ROUTINE_BUDGET_CHANGES: tuple[tuple[dt.date, str, str, str], ...] = (
    (dt.date(2026, 2, 10), "S02", "40.00", "44.00"),
    (dt.date(2026, 3, 5), "S08", "35.00", "38.00"),
    (dt.date(2026, 3, 20), "S14", "30.00", "33.00"),
    (dt.date(2026, 4, 15), "S04", "20.00", "18.00"),
    (dt.date(2026, 5, 12), "S10", "20.00", "22.00"),
    (dt.date(2026, 6, 2), "S16", "20.00", "22.00"),
)


def sort_events(events: list[ChangeEvent]) -> list[ChangeEvent]:
    """Events in canonical order, (date, entity_id); the sort is stable for ties."""
    return sorted(events, key=lambda e: (e.date, e.entity_id))


def baseline_events() -> list[ChangeEvent]:
    """Campaign launches plus the six routine budget changes, sorted by (date, entity_id)."""
    events = [
        ChangeEvent(
            date=c.start_date,
            entity_type="campaign",
            entity_id=c.campaign_id,
            change_type="launched",
            old_value=None,
            new_value=None,
        )
        for c in CAMPAIGNS
    ]
    events += [
        ChangeEvent(
            date=day,
            entity_type="ad_set",
            entity_id=ad_set_id,
            change_type="budget_change",
            old_value=old,
            new_value=new,
        )
        for day, ad_set_id, old, new in _ROUTINE_BUDGET_CHANGES
    ]
    return sort_events(events)


def events_for(
    entity_type: EntityType, entity_id: str, day: dt.date, events: list[ChangeEvent]
) -> list[ChangeEvent]:
    """Events on one entity that are in effect by `day`; a change applies ON its date."""
    return sort_events(
        [
            e
            for e in events
            if e.entity_type == entity_type and e.entity_id == entity_id and e.date <= day
        ]
    )


def budget_on(ad_set_id: str, day: dt.date, events: list[ChangeEvent]) -> float:
    """The ad set's daily budget in effect on `day`."""
    budget = AD_SETS_BY_ID[ad_set_id].daily_budget
    for event in events_for("ad_set", ad_set_id, day, events):
        if event.change_type == "budget_change" and event.new_value is not None:
            budget = float(event.new_value)
    return budget


def audience_on(ad_set_id: str, day: dt.date, events: list[ChangeEvent]) -> str:
    """The ad set's audience in effect on `day`."""
    audience = AD_SETS_BY_ID[ad_set_id].audience
    for event in events_for("ad_set", ad_set_id, day, events):
        if event.change_type == "audience_edit" and event.new_value is not None:
            audience = event.new_value
    return audience


def ad_active_on(ad: Ad, day: dt.date, events: list[ChangeEvent]) -> bool:
    """Whether the ad delivers on `day`: "paused" stops it and "resumed" restarts it."""
    active = True
    for event in events_for("ad", ad.ad_id, day, events):
        if event.change_type == "paused":
            active = False
        elif event.change_type == "resumed":
            active = True
    return active


def learning_cause(event: ChangeEvent) -> LearningCause | None:
    """The learning-phase cause a change on an ad set triggers, or None if it triggers none."""
    if event.change_type == "audience_edit":
        return "audience"
    if event.change_type == "budget_change":
        if event.old_value is None or event.new_value is None:
            return None
        old, new = float(event.old_value), float(event.new_value)
        if old == 0:
            return "budget" if new != 0 else None
        if abs(new - old) / old >= LEARNING_RESET_BUDGET_CHANGE:
            return "budget"
    return None


def resets_learning(event: ChangeEvent) -> bool:
    """Whether a change on an ad set sends its ads back into the learning phase."""
    return learning_cause(event) is not None


def learning_phase(
    ad: Ad, day: dt.date, events: list[ChangeEvent]
) -> tuple[dt.date, LearningCause]:
    """When the ad's current learning phase began, and what started it."""
    start: dt.date = ad.launched_date
    cause: LearningCause = "launch"
    for event in events_for("ad_set", ad.ad_set_id, day, events):
        event_cause = learning_cause(event)
        if event_cause is not None and event.date > start:
            start, cause = event.date, event_cause
    return start, cause


def learning_start(ad: Ad, day: dt.date, events: list[ChangeEvent]) -> dt.date:
    """When the ad's current learning phase began: launch, or the latest resetting change."""
    return learning_phase(ad, day, events)[0]
