SELECT f.week_start AS period,
       CAST(SUM(f.clicks) AS DOUBLE) / NULLIF(CAST(SUM(f.impr) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.ad_group = 'Beauty Interests'
  AND f.week_start BETWEEN DATE '2026-02-23' AND DATE '2026-04-05'
GROUP BY 1
ORDER BY 1;
