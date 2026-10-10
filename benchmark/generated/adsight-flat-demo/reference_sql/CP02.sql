SELECT strftime(f.week_start, '%Y-%m') AS label,
       SUM(f.clicks) AS value
FROM perf f
WHERE f.campaign_id = 'C1'
  AND f.week_start BETWEEN DATE '2026-03-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
