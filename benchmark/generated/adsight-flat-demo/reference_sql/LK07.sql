SELECT SUM(f.impr) AS impressions
FROM perf f
WHERE f.campaign_id = 'C3'
  AND f.week_start BETWEEN DATE '2026-05-30' AND DATE '2026-06-28';
