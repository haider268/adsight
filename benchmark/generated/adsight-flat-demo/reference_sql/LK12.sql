SELECT SUM(f.impr) AS impressions
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-01-05' AND DATE '2026-06-28';
