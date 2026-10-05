SELECT ad.headline AS key,
       ad.headline AS label,
       CAST(SUM(f.spend) AS DOUBLE) / NULLIF(CAST(SUM(f.leads) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE f.date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31'
GROUP BY 1, 2
ORDER BY value ASC, key;
