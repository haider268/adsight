SELECT f.ad_group AS key,
       f.ad_group AS label,
       SUM(f.impr) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-01-01' AND DATE '2026-03-31'
  AND f.campaign_id = 'C2'
GROUP BY 1, 2
ORDER BY value DESC, key;
