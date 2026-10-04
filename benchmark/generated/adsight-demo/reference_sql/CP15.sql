SELECT strftime(f.date, '%Y-%m') AS label,
       SUM(f.link_clicks) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
LEFT JOIN campaigns campaign ON campaign.campaign_id = ad_set.campaign_id
WHERE campaign.campaign_id = 'C5'
  AND f.date BETWEEN DATE '2026-01-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
