SELECT date_trunc('month', f.date) AS period,
       CAST(SUM(f.spend) AS DOUBLE) / NULLIF(CAST(SUM(f.leads) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
LEFT JOIN campaigns campaign ON campaign.campaign_id = ad_set.campaign_id
WHERE campaign.campaign_id = 'C8'
  AND f.date BETWEEN DATE '2026-02-01' AND DATE '2026-04-30'
GROUP BY 1
ORDER BY 1;
