SELECT ad_set.name AS label,
       CAST(SUM(f.impressions) AS DOUBLE) / NULLIF(CAST(SUM(f.reach) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
LEFT JOIN campaigns campaign ON campaign.campaign_id = ad_set.campaign_id
WHERE f.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-07'
  AND campaign.campaign_id = 'C5'
GROUP BY 1
ORDER BY 1;
