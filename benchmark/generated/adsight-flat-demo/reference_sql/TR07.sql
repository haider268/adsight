SELECT f.week_start AS period,
       CAST(SUM(f.cost) AS DOUBLE) / NULLIF(CAST(SUM(f.clicks) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.ad_group = 'Retargeting Quotes'
  AND f.week_start BETWEEN DATE '2026-06-01' AND DATE '2026-06-28'
GROUP BY 1
ORDER BY 1;
