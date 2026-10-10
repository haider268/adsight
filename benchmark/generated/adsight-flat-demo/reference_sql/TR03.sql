SELECT f.week_start AS period,
       CAST(SUM(f.conversions) AS DOUBLE) / NULLIF(CAST(SUM(f.clicks) AS DOUBLE), 0) AS value
FROM perf f
WHERE f.ad_group = 'Women 30-55 Broad'
  AND f.week_start BETWEEN DATE '2026-04-27' AND DATE '2026-06-07'
GROUP BY 1
ORDER BY 1;
