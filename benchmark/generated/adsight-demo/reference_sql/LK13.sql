SELECT SUM(f.link_clicks) AS link_clicks
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD035'
  AND f.date BETWEEN DATE '2026-05-04' AND DATE '2026-05-10';
