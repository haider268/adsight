SELECT f.week_start AS period,
       SUM(f.clicks) AS value
FROM perf f
WHERE f.ad_group = 'Retargeting Site'
  AND f.week_start BETWEEN DATE '2026-01-19' AND DATE '2026-03-01'
GROUP BY 1
ORDER BY 1;
