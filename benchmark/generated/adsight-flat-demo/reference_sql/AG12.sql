SELECT strftime(f.week_start, '%Y-%m') AS key,
       strftime(f.week_start, '%Y-%m') AS label,
       SUM(f.cost) AS value
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-01-01' AND DATE '2026-06-30'
  AND strftime(f.week_start, '%Y-%m') IN ('2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06')
GROUP BY 1, 2
ORDER BY value DESC, key;
