SELECT strftime(f.week_start, '%Y-%m') AS label,
       SUM(f.conversions) AS value
FROM perf f
WHERE f.campaign_id = 'C4'
  AND f.week_start BETWEEN DATE '2026-02-01' AND DATE '2026-06-30'
GROUP BY 1
ORDER BY 1;
