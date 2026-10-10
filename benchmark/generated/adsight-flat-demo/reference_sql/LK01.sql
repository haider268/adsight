SELECT SUM(f.cost) AS spend
FROM perf f
WHERE f.campaign_id = 'C2'
  AND f.week_start BETWEEN DATE '2026-02-01' AND DATE '2026-02-28';
