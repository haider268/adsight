SELECT date_trunc('month', f.week_start) AS period,
       CAST(SUM(f.cost) AS DOUBLE) * 1000.0 / NULLIF(CAST(SUM(f.impr) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.campaign_id = 'C3'
  AND f.week_start BETWEEN DATE '2026-02-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
