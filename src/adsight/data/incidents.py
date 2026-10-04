"""The five injected incidents: what happened, how it is built, and how it is measured.

Visible incidents add ordinary change events. Hidden ones (no events_log row) work through
the small effect helpers below, which the generator applies to every ad-day.
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict

from adsight.data.events import ChangeEvent

IncidentKind = Literal[
    "top_ad_paused", "budget_doubled", "creative_fatigue", "audience_edit", "tracking_outage"
]


class Incident(BaseModel):
    model_config = ConfigDict(frozen=True)

    incident_id: str
    kind: IncidentKind
    account_id: str
    campaign_id: str | None
    ad_set_id: str | None
    ad_id: str | None
    start_date: dt.date
    end_date: dt.date | None
    in_events_log: bool
    cause: str
    winner_cvr_multiplier: float | None = None
    freq_extra_per_day: float | None = None
    freq_cap: float | None = None
    lead_multiplier: float | None = None
    metric: Literal["cpl", "ctr", "lead_rate"]
    scope_type: Literal["ad", "ad_set", "account"]
    scope_id: str
    before: tuple[dt.date, dt.date]
    after: tuple[dt.date, dt.date]


def _d(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso)


INCIDENTS: tuple[Incident, ...] = (
    Incident(
        incident_id="I1",
        kind="top_ad_paused",
        account_id="A2",
        campaign_id="C4",
        ad_set_id="S07",
        ad_id="AD019",
        start_date=_d("2026-03-16"),
        end_date=None,
        in_events_log=True,
        cause=(
            "AD019, the best-converting ad in S07, was paused on 2026-03-16 and its budget "
            "moved to the two weaker ads in the ad set."
        ),
        winner_cvr_multiplier=1.65,
        metric="cpl",
        scope_type="ad_set",
        scope_id="S07",
        before=(_d("2026-03-02"), _d("2026-03-15")),
        after=(_d("2026-03-16"), _d("2026-03-29")),
    ),
    Incident(
        incident_id="I2",
        kind="budget_doubled",
        account_id="A3",
        campaign_id="C7",
        ad_set_id="S13",
        ad_id=None,
        start_date=_d("2026-04-20"),
        end_date=None,
        in_events_log=True,
        cause=(
            "S13's daily budget was doubled from 55.00 to 110.00 on 2026-04-20; the extra "
            "spend bought pricier reach, so cost per lead rose."
        ),
        metric="cpl",
        scope_type="ad_set",
        scope_id="S13",
        before=(_d("2026-04-06"), _d("2026-04-19")),
        after=(_d("2026-04-27"), _d("2026-05-10")),
    ),
    Incident(
        incident_id="I3",
        kind="creative_fatigue",
        account_id="A1",
        campaign_id="C1",
        ad_set_id="S01",
        ad_id="AD001",
        start_date=_d("2026-05-04"),
        end_date=None,
        in_events_log=False,
        cause=(
            "AD001 was shown to the same people more and more often from 2026-05-04; once "
            "its frequency passed 2.0 its click-through rate fell. No change was made to it."
        ),
        freq_extra_per_day=0.04,
        freq_cap=4.5,
        metric="ctr",
        scope_type="ad",
        scope_id="AD001",
        before=(_d("2026-04-20"), _d("2026-05-03")),
        after=(_d("2026-06-16"), _d("2026-06-29")),
    ),
    Incident(
        incident_id="I4",
        kind="audience_edit",
        account_id="A2",
        campaign_id="C5",
        ad_set_id="S09",
        ad_id=None,
        start_date=_d("2026-05-18"),
        end_date=_d("2026-05-24"),
        in_events_log=True,
        cause=(
            "S09's audience was changed from lookalike_1pct to lookalike_3pct on "
            "2026-05-18, which restarted the learning phase; cost per lead was much higher "
            "for the following 7 days."
        ),
        metric="cpl",
        scope_type="ad_set",
        scope_id="S09",
        before=(_d("2026-05-04"), _d("2026-05-17")),
        after=(_d("2026-05-18"), _d("2026-05-24")),
    ),
    Incident(
        incident_id="I5",
        kind="tracking_outage",
        account_id="A3",
        campaign_id=None,
        ad_set_id=None,
        ad_id=None,
        start_date=_d("2026-06-08"),
        end_date=_d("2026-06-09"),
        in_events_log=False,
        cause=(
            "Lead tracking broke for the whole A3 account on 2026-06-08 and 2026-06-09: "
            "clicks stayed normal but almost no leads were recorded. No change was made in "
            "the account."
        ),
        lead_multiplier=0.05,
        metric="lead_rate",
        scope_type="account",
        scope_id="A3",
        before=(_d("2026-05-25"), _d("2026-06-07")),
        after=(_d("2026-06-08"), _d("2026-06-09")),
    ),
)


def incident_events(incidents: list[Incident] | tuple[Incident, ...]) -> list[ChangeEvent]:
    """The events_log rows the visible incidents leave behind."""
    events: list[ChangeEvent] = []
    for inc in incidents:
        if not inc.in_events_log:
            continue
        if inc.kind == "top_ad_paused" and inc.ad_id is not None:
            events.append(
                ChangeEvent(
                    date=inc.start_date,
                    entity_type="ad",
                    entity_id=inc.ad_id,
                    change_type="paused",
                    old_value="active",
                    new_value="paused",
                )
            )
        elif inc.kind == "budget_doubled" and inc.ad_set_id is not None:
            events.append(
                ChangeEvent(
                    date=inc.start_date,
                    entity_type="ad_set",
                    entity_id=inc.ad_set_id,
                    change_type="budget_change",
                    old_value="55.00",
                    new_value="110.00",
                )
            )
        elif inc.kind == "audience_edit" and inc.ad_set_id is not None:
            events.append(
                ChangeEvent(
                    date=inc.start_date,
                    entity_type="ad_set",
                    entity_id=inc.ad_set_id,
                    change_type="audience_edit",
                    old_value="lookalike_1pct",
                    new_value="lookalike_3pct",
                )
            )
    return events


def _covers(inc: Incident, day: dt.date) -> bool:
    return inc.start_date <= day and (inc.end_date is None or day <= inc.end_date)


def winner_multiplier(ad_id: str, incidents: list[Incident] | tuple[Incident, ...]) -> float:
    """Lifelong CVR multiplier for an ad that is secretly the ad set's best converter."""
    multiplier = 1.0
    for inc in incidents:
        if inc.ad_id == ad_id and inc.winner_cvr_multiplier is not None:
            multiplier *= inc.winner_cvr_multiplier
    return multiplier


def extra_frequency(
    ad_id: str, day: dt.date, incidents: list[Incident] | tuple[Incident, ...]
) -> tuple[float, float | None]:
    """Extra frequency piling up on a fatiguing ad, and the cap that then applies."""
    for inc in incidents:
        if inc.ad_id == ad_id and inc.freq_extra_per_day is not None and day >= inc.start_date:
            return inc.freq_extra_per_day * (day - inc.start_date).days, inc.freq_cap
    return 0.0, None


def lead_multiplier(
    account_id: str, day: dt.date, incidents: list[Incident] | tuple[Incident, ...]
) -> float:
    """Share of leads that get recorded: below 1.0 only during a tracking outage."""
    multiplier = 1.0
    for inc in incidents:
        if inc.account_id == account_id and inc.lead_multiplier is not None and _covers(inc, day):
            multiplier *= inc.lead_multiplier
    return multiplier
