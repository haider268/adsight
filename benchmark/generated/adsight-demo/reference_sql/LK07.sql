SELECT SUM(f.impressions) AS impressions
FROM daily_ad_metrics f
WHERE f.date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
