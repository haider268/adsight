SELECT date_trunc('week', f.date) AS period,
       SUM(f.spend) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD008'
  AND f.date BETWEEN DATE '2026-02-23' AND DATE '2026-03-22'
GROUP BY 1
ORDER BY 1;
