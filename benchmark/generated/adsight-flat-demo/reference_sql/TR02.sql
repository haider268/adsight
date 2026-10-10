SELECT date_trunc('month', f.week_start) AS period,
       SUM(f.impr) AS value
FROM perf f
WHERE f.campaign_id = 'C6'
  AND f.week_start BETWEEN DATE '2026-04-01' AND DATE '2026-06-30'
GROUP BY 1
ORDER BY 1;
