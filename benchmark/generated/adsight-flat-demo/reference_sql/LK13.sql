SELECT SUM(f.impr) AS impressions
FROM perf f
WHERE f.campaign_id = 'C4'
  AND f.week_start BETWEEN DATE '2026-06-01' AND DATE '2026-06-30';
