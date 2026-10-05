SELECT date_trunc('week', f.date) AS period,
       CAST(SUM(f.impressions) AS DOUBLE) / NULLIF(CAST(SUM(f.reach) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE ad_set.ad_set_id = 'S10'
  AND f.date BETWEEN DATE '2026-01-12' AND DATE '2026-02-22'
GROUP BY 1
ORDER BY 1;
