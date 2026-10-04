SELECT CAST(SUM(f.impressions) AS DOUBLE) / NULLIF(CAST(SUM(f.reach) AS DOUBLE), 0) AS frequency
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD036'
  AND f.date BETWEEN DATE '2026-03-01' AND DATE '2026-03-31';
