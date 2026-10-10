SELECT f.ad_group AS key,
       f.ad_group AS label,
       SUM(f.cost) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-04-01' AND DATE '2026-06-30'
  AND f.campaign_id = 'C7'
GROUP BY 1, 2
ORDER BY value ASC, key;
