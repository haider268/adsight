SELECT CAST(SUM(f.impressions) AS DOUBLE) / COUNT(DISTINCT f.date) AS impressions
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD035'
  AND f.date BETWEEN DATE '2026-04-01' AND DATE '2026-04-30';
