SELECT ad_set.placement AS label,
       SUM(f.spend) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE f.date BETWEEN DATE '2026-03-23' AND DATE '2026-03-29'
GROUP BY 1
ORDER BY 1;
