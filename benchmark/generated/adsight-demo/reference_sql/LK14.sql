SELECT CAST(SUM(f.spend) AS DOUBLE) / NULLIF(CAST(SUM(f.clicks) AS DOUBLE), 0) AS cpc
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
WHERE ad.ad_id = 'AD021'
  AND f.date BETWEEN DATE '2026-03-02' AND DATE '2026-03-08';
