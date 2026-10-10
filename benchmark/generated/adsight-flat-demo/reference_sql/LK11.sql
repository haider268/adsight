SELECT CAST(SUM(f.cost) AS DOUBLE) * 1000.0 / NULLIF(CAST(SUM(f.impr) AS DOUBLE), 0) AS cpm
FROM perf f
WHERE f.ad_group = 'Retargeting Quotes'
  AND f.week_start BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
