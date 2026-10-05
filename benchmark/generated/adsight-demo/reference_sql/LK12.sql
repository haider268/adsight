SELECT CAST(SUM(f.link_clicks) AS DOUBLE) / COUNT(DISTINCT f.date) AS link_clicks
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE ad_set.ad_set_id = 'S13'
  AND f.date BETWEEN DATE '2026-05-31' AND DATE '2026-06-29';
