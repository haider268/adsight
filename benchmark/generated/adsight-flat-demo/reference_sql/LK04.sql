SELECT SUM(f.clicks) AS clicks
FROM perf f
WHERE f.ad_group = 'New Movers'
  AND f.week_start BETWEEN DATE '2026-04-01' AND DATE '2026-06-30';
