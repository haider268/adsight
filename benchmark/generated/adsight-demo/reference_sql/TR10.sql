SELECT date_trunc('month', f.date) AS period,
       CAST(SUM(f.spend) AS DOUBLE) / NULLIF(CAST(SUM(f.leads) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD046'
  AND f.date BETWEEN DATE '2026-01-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
