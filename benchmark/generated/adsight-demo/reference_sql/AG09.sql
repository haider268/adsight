SELECT ad.status AS key,
       ad.status AS label,
       CAST(SUM(f.impressions) AS DOUBLE) / NULLIF(CAST(SUM(f.reach) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE f.date BETWEEN DATE '2026-01-19' AND DATE '2026-01-25'
GROUP BY 1, 2
ORDER BY value ASC, key;
