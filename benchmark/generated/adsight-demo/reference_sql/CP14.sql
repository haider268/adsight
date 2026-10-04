SELECT account.vertical AS label,
       SUM(f.clicks) AS value
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
LEFT JOIN campaigns campaign ON campaign.campaign_id = ad_set.campaign_id
LEFT JOIN accounts account ON account.account_id = campaign.account_id
WHERE f.date BETWEEN DATE '2026-03-09' AND DATE '2026-03-15'
GROUP BY 1
ORDER BY 1;
