SELECT f.week_start AS period,
       SUM(f.clicks) AS value
FROM perf f
WHERE f.campaign_id = 'C1'
  AND f.week_start BETWEEN DATE '2026-02-02' AND DATE '2026-03-01'
GROUP BY 1
ORDER BY 1;
