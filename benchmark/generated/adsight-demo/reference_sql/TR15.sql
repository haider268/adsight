SELECT date_trunc('week', f.date) AS period,
       CAST(SUM(f.spend) AS DOUBLE) * 1000.0 / NULLIF(CAST(SUM(f.impressions) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE ad_set.ad_set_id = 'S14'
  AND f.date BETWEEN DATE '2026-04-20' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
