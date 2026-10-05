SELECT CAST(SUM(f.spend) AS DOUBLE) / COUNT(DISTINCT f.date) AS spend
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
LEFT JOIN campaigns campaign ON campaign.campaign_id = ad_set.campaign_id
WHERE campaign.campaign_id = 'C4'
  AND f.date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
