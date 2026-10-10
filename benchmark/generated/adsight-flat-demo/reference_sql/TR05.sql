SELECT f.week_start AS period,
       SUM(f.cost) AS value
FROM perf f
WHERE f.campaign_id = 'C4'
  AND f.week_start BETWEEN DATE '2026-01-26' AND DATE '2026-02-22'
GROUP BY 1
ORDER BY 1;
