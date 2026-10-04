SELECT ad.name AS label,
       CAST(SUM(f.clicks) AS DOUBLE) / NULLIF(CAST(SUM(f.impressions) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE f.date BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
  AND ad_set.ad_set_id = 'S02'
GROUP BY 1
ORDER BY 1;
