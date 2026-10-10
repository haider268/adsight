SELECT SUM(f.cost) AS spend
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-05-30' AND DATE '2026-06-28';
