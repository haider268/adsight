SELECT CAST(SUM(f.link_clicks) AS DOUBLE) / COUNT(DISTINCT f.date) AS link_clicks
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD006'
  AND f.date BETWEEN DATE '2026-03-02' AND DATE '2026-03-08';
