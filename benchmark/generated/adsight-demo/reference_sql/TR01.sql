SELECT date_trunc('week', f.date) AS period,
       SUM(f.spend) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
WHERE ad_set.ad_set_id = 'S16'
  AND f.date BETWEEN DATE '2026-02-23' AND DATE '2026-04-05'
GROUP BY 1
ORDER BY 1;
