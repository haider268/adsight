"""The fixed, fictional entity catalog and the model constants for the generator."""

from dataclasses import dataclass
from datetime import date, timedelta

WINDOW_START = date(2026, 1, 1)
WINDOW_DAYS = 180
WINDOW_END = WINDOW_START + timedelta(days=WINDOW_DAYS - 1)


@dataclass(frozen=True)
class Vertical:
    name: str
    base_cpm: float
    base_ctr: float
    base_cvr: float
    weekday_cvr: tuple[float, float, float, float, float, float, float]


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    vertical: str


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    account_id: str
    name: str
    objective: str
    status: str
    start_date: date


@dataclass(frozen=True)
class AdSet:
    ad_set_id: str
    campaign_id: str
    name: str
    audience: str
    placement: str
    daily_budget: float


@dataclass(frozen=True)
class Ad:
    ad_id: str
    ad_set_id: str
    name: str
    creative_type: str
    headline: str
    launched_date: date


VERTICALS: dict[str, Vertical] = {
    v.name: v
    for v in (
        Vertical("regenerative_clinic", 14.0, 0.018, 0.045, (1, 1, 1, 1, 1, 0.8, 0.7)),
        Vertical("med_spa", 12.0, 0.022, 0.060, (1, 1, 1, 1, 1, 0.9, 0.8)),
        Vertical("home_services", 10.0, 0.015, 0.080, (1, 1, 1, 1, 1, 1.05, 0.95)),
    )
}

ACCOUNTS: tuple[Account, ...] = (
    Account("A1", "Restore Regenerative Clinic", "regenerative_clinic"),
    Account("A2", "Lumen Med Spa", "med_spa"),
    Account("A3", "Northside Home Services", "home_services"),
)

CAMPAIGNS: tuple[Campaign, ...] = tuple(
    Campaign(cid, acc, name, objective, "active", start)
    for cid, acc, name, objective, start in (
        ("C1", "A1", "Knee Pain PRP", "leads", date(2026, 1, 1)),
        ("C2", "A1", "Back Pain Regenerative", "leads", date(2026, 1, 1)),
        ("C3", "A1", "Clinic Awareness", "traffic", date(2026, 2, 1)),
        ("C4", "A2", "Botox New Clients", "leads", date(2026, 1, 1)),
        ("C5", "A2", "Laser Hair Removal", "leads", date(2026, 1, 1)),
        ("C6", "A2", "Membership Promo", "leads", date(2026, 3, 1)),
        ("C7", "A3", "HVAC Tune-Up", "leads", date(2026, 1, 1)),
        ("C8", "A3", "Water Heater Replacement", "leads", date(2026, 1, 1)),
        ("C9", "A3", "Roofing Inspection", "traffic", date(2026, 4, 1)),
    )
)

AD_SETS: tuple[AdSet, ...] = tuple(
    AdSet(sid, cid, name, audience, placement, float(budget))
    for sid, cid, name, audience, placement, budget in (
        ("S01", "C1", "Knee 45-65 Broad", "broad_45_65", "advantage_plus", 60),
        ("S02", "C1", "Knee Interest Stack", "interest_joint_health", "feeds", 40),
        ("S03", "C2", "Back Pain Lookalike", "lookalike_1pct", "advantage_plus", 50),
        ("S04", "C2", "Back Pain Retargeting", "retargeting_30d", "feeds", 20),
        ("S05", "C3", "Local Radius 15mi", "local_radius", "reels", 25),
        ("S06", "C3", "Video Viewers", "retargeting_video", "stories", 15),
        ("S07", "C4", "Women 30-55 Broad", "broad_30_55", "advantage_plus", 70),
        ("S08", "C4", "Beauty Interests", "interest_beauty", "feeds", 35),
        ("S09", "C5", "Lookalike Bookers", "lookalike_1pct", "advantage_plus", 45),
        ("S10", "C5", "Retargeting Site", "retargeting_30d", "feeds", 20),
        ("S11", "C6", "Past Clients", "customer_list", "feeds", 30),
        ("S12", "C6", "Local Radius 10mi", "local_radius", "reels", 25),
        ("S13", "C7", "Homeowners 35-65", "broad_35_65", "advantage_plus", 55),
        ("S14", "C7", "New Movers", "interest_new_movers", "feeds", 30),
        ("S15", "C8", "Lookalike Customers", "lookalike_1pct", "advantage_plus", 40),
        ("S16", "C8", "Retargeting Quotes", "retargeting_30d", "feeds", 20),
        ("S17", "C9", "Storm Zip Codes", "local_radius", "feeds", 35),
        ("S18", "C9", "Video Viewers", "retargeting_video", "reels", 15),
    )
)

CREATIVE_TYPES: tuple[str, str, str] = ("video", "image", "carousel")

HEADLINES: dict[str, tuple[str, str, str]] = {
    "regenerative_clinic": (
        "Move without joint pain",
        "Non-surgical relief options",
        "Book a free assessment",
    ),
    "med_spa": (
        "Look refreshed this season",
        "New client special",
        "Book with a licensed injector",
    ),
    "home_services": (
        "Same-week service",
        "Upfront pricing, no surprises",
        "Book a free inspection",
    ),
}

CREATIVE_CTR = {"video": 1.10, "image": 1.00, "carousel": 0.90}
CREATIVE_SPEND_SHARE = {"video": 0.45, "image": 0.35, "carousel": 0.20}
OBJECTIVE_CVR = {"leads": 1.0, "traffic": 0.3}
LINK_SHARE = 0.65
FATIGUE_START = 2.0
FATIGUE_K = 0.5
FREQ_CAP = 2.6
LEARNING_DAYS = 7
DIMINISHING_EXPONENT = 0.3
LEARNING_RESET_BUDGET_CHANGE = 0.20


@dataclass(frozen=True)
class LearningProfile:
    """How an ad behaves while learning: CVR ramps from `floor` to 1 over LEARNING_DAYS."""

    floor: float
    noise_sd: float
    cpm_multiplier: float


# Keyed by what started the learning phase. An audience edit restarts learning harder
# than a launch or a budget change.
LEARNING_PROFILES: dict[str, LearningProfile] = {
    "launch": LearningProfile(0.7, 0.35, 1.0),
    "budget": LearningProfile(0.7, 0.35, 1.0),
    "audience": LearningProfile(0.4, 0.50, 1.25),
}

_AUDIENCE_FREQ_PREFIXES: tuple[tuple[str, float], ...] = (
    ("retargeting_", 3.0),
    ("customer_list", 3.0),
    ("local_radius", 1.5),
    ("interest_", 1.0),
    ("lookalike_", 1.0),
    ("broad_", 0.5),
)


def audience_freq_factor(audience: str) -> float:
    """How fast frequency builds for an audience: small audiences saturate faster."""
    for prefix, factor in _AUDIENCE_FREQ_PREFIXES:
        if audience.startswith(prefix):
            return factor
    raise ValueError(f"unknown audience: {audience!r}")


ACCOUNTS_BY_ID = {a.account_id: a for a in ACCOUNTS}
CAMPAIGNS_BY_ID = {c.campaign_id: c for c in CAMPAIGNS}
AD_SETS_BY_ID = {s.ad_set_id: s for s in AD_SETS}


def _build_ads() -> tuple[Ad, ...]:
    ads: list[Ad] = []
    for ad_set in AD_SETS:
        campaign = CAMPAIGNS_BY_ID[ad_set.campaign_id]
        vertical = ACCOUNTS_BY_ID[campaign.account_id].vertical
        for position, creative_type in enumerate(CREATIVE_TYPES):
            ads.append(
                Ad(
                    ad_id=f"AD{len(ads) + 1:03d}",
                    ad_set_id=ad_set.ad_set_id,
                    name=f"{ad_set.name} | {creative_type.title()}",
                    creative_type=creative_type,
                    headline=HEADLINES[vertical][position],
                    launched_date=campaign.start_date,
                )
            )
    return tuple(ads)


ADS: tuple[Ad, ...] = _build_ads()
ADS_BY_ID = {a.ad_id: a for a in ADS}


def account_for(campaign: Campaign) -> Account:
    return ACCOUNTS_BY_ID[campaign.account_id]


def campaign_for(ad_set: AdSet) -> Campaign:
    return CAMPAIGNS_BY_ID[ad_set.campaign_id]


def ad_set_for(ad: Ad) -> AdSet:
    return AD_SETS_BY_ID[ad.ad_set_id]


def vertical_of(ad: Ad) -> Vertical:
    return VERTICALS[account_for(campaign_for(ad_set_for(ad))).vertical]
