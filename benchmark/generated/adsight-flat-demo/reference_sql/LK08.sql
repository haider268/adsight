SELECT CAST(SUM(f.cost) AS DOUBLE) / NULLIF(CAST(SUM(f.conversions) AS DOUBLE), 0) AS cpl
FROM perf f
WHERE f.ad_group = 'Women 30-55 Broad'
  AND f.week_start BETWEEN DATE '2026-05-30' AND DATE '2026-06-28';
