SELECT date_trunc('week', f.date) AS period,
       SUM(f.impressions) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE ad_set.ad_set_id = 'S06'
  AND f.date BETWEEN DATE '2026-02-02' AND DATE '2026-03-29'
GROUP BY 1
ORDER BY 1;
