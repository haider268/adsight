SELECT CAST(SUM(f.spend) AS DOUBLE) * 1000.0 / NULLIF(CAST(SUM(f.impressions) AS DOUBLE), 0) AS cpm
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD022'
  AND f.date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
