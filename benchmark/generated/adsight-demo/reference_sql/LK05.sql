SELECT SUM(f.leads) AS conversions
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD048'
  AND f.date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
