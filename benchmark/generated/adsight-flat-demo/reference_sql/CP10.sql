SELECT strftime(f.week_start, '%Y-%m') AS label,
       SUM(f.clicks) AS value
FROM perf f
WHERE f.ad_group = 'Retargeting Site'
  AND f.week_start BETWEEN DATE '2026-02-01' AND DATE '2026-06-30'
GROUP BY 1
ORDER BY 1;
