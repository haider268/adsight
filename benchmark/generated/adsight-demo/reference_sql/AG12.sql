SELECT campaign.objective AS key,
       campaign.objective AS label,
       CAST(SUM(f.spend) AS DOUBLE) * 1000.0 / NULLIF(CAST(SUM(f.impressions) AS DOUBLE), 0) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
LEFT JOIN campaigns campaign ON campaign.campaign_id = ad_set.campaign_id
WHERE f.date BETWEEN DATE '2026-02-16' AND DATE '2026-02-22'
GROUP BY 1, 2
ORDER BY value ASC, key;
