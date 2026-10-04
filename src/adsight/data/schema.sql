-- One row per ad account: one fictional business advertising on Meta.
CREATE TABLE accounts (
    account_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    vertical VARCHAR NOT NULL
);

-- One row per campaign inside an account.
CREATE TABLE campaigns (
    campaign_id VARCHAR PRIMARY KEY,
    account_id VARCHAR NOT NULL REFERENCES accounts,
    name VARCHAR NOT NULL,
    objective VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    start_date DATE NOT NULL
);

-- One row per ad set; daily_budget is the USD budget at the END of the window.
CREATE TABLE ad_sets (
    ad_set_id VARCHAR PRIMARY KEY,
    campaign_id VARCHAR NOT NULL REFERENCES campaigns,
    name VARCHAR NOT NULL,
    audience VARCHAR NOT NULL,
    placement VARCHAR NOT NULL,
    daily_budget DECIMAL(12,2) NOT NULL
);

-- One row per ad (one creative) inside an ad set; status is "active" or "paused" at window end.
CREATE TABLE ads (
    ad_id VARCHAR PRIMARY KEY,
    ad_set_id VARCHAR NOT NULL REFERENCES ad_sets,
    name VARCHAR NOT NULL,
    creative_type VARCHAR NOT NULL,
    headline VARCHAR NOT NULL,
    launched_date DATE NOT NULL,
    status VARCHAR NOT NULL
);

-- One row per ad per delivery day; spend is USD.
CREATE TABLE daily_ad_metrics (
    date DATE NOT NULL,
    ad_id VARCHAR NOT NULL REFERENCES ads,
    impressions INTEGER NOT NULL,
    reach INTEGER NOT NULL,
    clicks INTEGER NOT NULL,
    link_clicks INTEGER NOT NULL,
    spend DECIMAL(12,2) NOT NULL,
    leads INTEGER NOT NULL,
    frequency DOUBLE NOT NULL,
    PRIMARY KEY (date, ad_id)
);

-- One row per change made to an entity: launch, budget change, pause and so on.
CREATE TABLE events_log (
    event_id INTEGER PRIMARY KEY,
    date DATE NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_id VARCHAR NOT NULL,
    change_type VARCHAR NOT NULL,
    old_value VARCHAR,
    new_value VARCHAR
);
