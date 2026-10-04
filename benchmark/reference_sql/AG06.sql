SELECT creative_type, creative_type AS name, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
GROUP BY 1
ORDER BY v ASC;
