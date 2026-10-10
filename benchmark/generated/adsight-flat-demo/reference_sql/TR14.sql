SELECT date_trunc('month', f.week_start) AS period,
       SUM(f.impr) AS value
FROM perf f
WHERE f.ad_group = 'Retargeting Quotes'
  AND f.week_start BETWEEN DATE '2026-03-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
