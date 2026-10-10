SELECT f.ad_group AS key,
       f.ad_group AS label,
       SUM(f.impr) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-05-30' AND DATE '2026-06-28'
  AND f.campaign_id = 'C4'
GROUP BY 1, 2
ORDER BY value ASC, key;
