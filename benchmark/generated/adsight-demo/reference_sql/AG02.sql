SELECT ad.creative_type AS key,
       ad.creative_type AS label,
       SUM(f.spend) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE f.date BETWEEN DATE '2026-01-12' AND DATE '2026-01-18'
GROUP BY 1, 2
ORDER BY value ASC, key;
