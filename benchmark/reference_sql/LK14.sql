SELECT ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaign_id = 'C4';
