SELECT ad_set.placement AS key,
       ad_set.placement AS label,
       SUM(f.leads) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE f.date BETWEEN DATE '2026-01-01' AND DATE '2026-01-31'
GROUP BY 1, 2
ORDER BY value ASC, key;
