SELECT f.week_start AS period,
       SUM(f.conversions) AS value
FROM perf f
WHERE f.ad_group = 'Knee 45-65 Broad'
  AND f.week_start BETWEEN DATE '2026-01-19' AND DATE '2026-03-15'
GROUP BY 1
ORDER BY 1;
