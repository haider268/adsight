SELECT SUM(f.clicks) AS clicks
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
