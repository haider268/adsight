SELECT SUM(f.clicks) AS clicks
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD005'
  AND f.date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31';
