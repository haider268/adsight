SELECT SUM(f.cost) AS spend
FROM perf f
WHERE f.campaign_id = 'C3'
  AND f.week_start BETWEEN DATE '2026-01-05' AND DATE '2026-06-28';
