SELECT ad.ad_id AS key,
       ad.name AS label,
       SUM(f.spend) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE f.date BETWEEN DATE '2026-06-08' AND DATE '2026-06-14'
  AND ad_set.ad_set_id = 'S02'
GROUP BY 1, 2
ORDER BY value DESC, key;
