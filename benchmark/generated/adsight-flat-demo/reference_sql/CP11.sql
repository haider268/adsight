SELECT f.ad_group AS label,
       CAST(SUM(f.conversions) AS DOUBLE) / NULLIF(CAST(SUM(f.clicks) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-01-01' AND DATE '2026-03-31'
  AND f.campaign_id = 'C2'
GROUP BY 1
ORDER BY 1;
