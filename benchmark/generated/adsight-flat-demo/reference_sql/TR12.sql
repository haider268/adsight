SELECT date_trunc('month', f.week_start) AS period,
       CAST(SUM(f.cost) AS DOUBLE) * 1000.0 / NULLIF(CAST(SUM(f.impr) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.ad_group = 'Video Viewers'
  AND f.week_start BETWEEN DATE '2026-02-01' AND DATE '2026-06-30'
GROUP BY 1
ORDER BY 1;
