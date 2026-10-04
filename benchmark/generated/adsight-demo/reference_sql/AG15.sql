SELECT ad_set.audience AS key,
       ad_set.audience AS label,
       SUM(f.clicks) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE f.date BETWEEN DATE '2026-01-19' AND DATE '2026-01-25'
GROUP BY 1, 2
ORDER BY value DESC, key;
