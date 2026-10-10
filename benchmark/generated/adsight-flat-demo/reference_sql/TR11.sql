SELECT f.week_start AS period,
       SUM(f.clicks) AS value
FROM perf f
WHERE f.ad_group = 'Knee Interest Stack'
  AND f.week_start BETWEEN DATE '2026-01-19' AND DATE '2026-02-15'
GROUP BY 1
ORDER BY 1;
