SELECT strftime(f.date, '%Y-%m') AS label,
       SUM(f.clicks) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD009'
  AND f.date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31'
GROUP BY 1
ORDER BY 1;
