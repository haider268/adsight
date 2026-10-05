SELECT ad.creative_type AS label,
       CAST(SUM(f.spend) AS DOUBLE) / NULLIF(CAST(SUM(f.leads) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE f.date BETWEEN DATE '2026-05-31' AND DATE '2026-06-29'
GROUP BY 1
ORDER BY 1;
