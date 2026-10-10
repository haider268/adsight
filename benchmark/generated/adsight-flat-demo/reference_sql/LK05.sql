SELECT CAST(SUM(f.conversions) AS DOUBLE) / NULLIF(CAST(SUM(f.clicks) AS DOUBLE), 0) AS cvr
FROM perf f
WHERE f.ad_group = 'Retargeting Site'
  AND f.week_start BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
