SELECT f.ad_group AS label,
       SUM(f.conversions) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-01-05' AND DATE '2026-06-28'
  AND f.campaign_id = 'C2'
GROUP BY 1
ORDER BY 1;
