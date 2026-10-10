SELECT date_trunc('month', f.week_start) AS period,
       CAST(SUM(f.cost) AS DOUBLE) / NULLIF(CAST(SUM(f.conversions) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.campaign_id = 'C4'
  AND f.week_start BETWEEN DATE '2026-01-01' AND DATE '2026-04-30'
GROUP BY 1
ORDER BY 1;
