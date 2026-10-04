import pytest

from adsight.data.catalog import (
    ACCOUNTS,
    AD_SETS,
    ADS,
    ADS_BY_ID,
    CAMPAIGNS,
    WINDOW_END,
    account_for,
    ad_set_for,
    audience_freq_factor,
    campaign_for,
    vertical_of,
)


def test_entity_counts() -> None:
    assert len(ACCOUNTS) == 3
    assert len(CAMPAIGNS) == 9
    assert len(AD_SETS) == 18
    assert len(ADS) == 54


def test_ad_ids_are_sequential() -> None:
    assert [a.ad_id for a in ADS] == [f"AD{i:03d}" for i in range(1, 55)]


def test_window_end() -> None:
    assert WINDOW_END.isoformat() == "2026-06-29"


def test_three_creatives_per_ad_set_in_order() -> None:
    for ad_set in AD_SETS:
        types = [a.creative_type for a in ADS if a.ad_set_id == ad_set.ad_set_id]
        assert types == ["video", "image", "carousel"]


def test_ad_fields_follow_catalog() -> None:
    ad = ADS_BY_ID["AD002"]
    assert ad.name == "Knee 45-65 Broad | Image"
    assert ad.headline == "Non-surgical relief options"
    assert ad.launched_date.isoformat() == "2026-01-01"
    late = ADS_BY_ID["AD054"]
    assert late.ad_set_id == "S18"
    assert late.launched_date.isoformat() == "2026-04-01"
    assert late.headline == "Book a free inspection"


def test_lookup_helpers() -> None:
    ad = ADS_BY_ID["AD022"]
    ad_set = ad_set_for(ad)
    campaign = campaign_for(ad_set)
    assert ad_set.ad_set_id == "S08"
    assert campaign.campaign_id == "C4"
    assert account_for(campaign).name == "Lumen Med Spa"
    assert vertical_of(ad).name == "med_spa"


@pytest.mark.parametrize(
    ("audience", "factor"),
    [
        ("retargeting_30d", 3.0),
        ("customer_list", 3.0),
        ("local_radius", 1.5),
        ("interest_beauty", 1.0),
        ("lookalike_1pct", 1.0),
        ("broad_45_65", 0.5),
    ],
)
def test_audience_freq_factor(audience: str, factor: float) -> None:
    assert audience_freq_factor(audience) == factor


def test_unknown_audience_raises() -> None:
    with pytest.raises(ValueError, match="unknown audience"):
        audience_freq_factor("everyone")
