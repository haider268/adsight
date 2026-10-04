SELECT ROUND(SUM(m.spend) / COUNT(DISTINCT m.date), 2) AS avg_daily_spend
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaign_id = 'C3'
  AND m.date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31';
