SELECT ROUND(SUM(m.clicks) / SUM(m.impressions), 4) AS ctr
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaign_id = 'C7'
  AND m.date BETWEEN DATE '2026-01-01' AND DATE '2026-01-31';
