SELECT f.campaign_id AS key,
       f.campaign_name AS label,
       SUM(f.cost) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-05-30' AND DATE '2026-06-28'
GROUP BY 1, 2
ORDER BY value DESC, key;
