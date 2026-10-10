SELECT f.ad_group AS label,
       CAST(SUM(f.cost) AS DOUBLE) / NULLIF(CAST(SUM(f.clicks) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-05-30' AND DATE '2026-06-28'
  AND f.campaign_id = 'C1'
GROUP BY 1
ORDER BY 1;
