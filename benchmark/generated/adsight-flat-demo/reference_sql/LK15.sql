SELECT SUM(f.conversions) AS conversions
FROM perf f
WHERE f.week_start BETWEEN DATE '2026-02-01' AND DATE '2026-02-28';
