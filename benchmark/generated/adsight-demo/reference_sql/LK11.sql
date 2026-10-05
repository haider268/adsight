SELECT SUM(f.leads) AS conversions
FROM daily_ad_metrics f
WHERE f.date BETWEEN DATE '2026-05-31' AND DATE '2026-06-29';
