SELECT ROUND(SUM(m.impressions) / SUM(m.reach), 3) AS frequency
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE ad_set_id = 'S06'
  AND m.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-29';
