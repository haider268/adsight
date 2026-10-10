SELECT f.ad_group AS key,
       f.ad_group AS label,
       SUM(f.impr) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-02-01' AND DATE '2026-02-28'
  AND f.campaign_id = 'C8'
GROUP BY 1, 2
ORDER BY value ASC, key;
