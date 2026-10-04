SELECT ROUND(SUM(m.spend), 2) AS spend
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id);
