SELECT SUM(f.spend) AS spend
FROM daily_ad_metrics f
WHERE f.date BETWEEN DATE '2026-05-11' AND DATE '2026-05-17';
