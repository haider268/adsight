SELECT CAST(SUM(f.leads) AS DOUBLE) / NULLIF(CAST(SUM(f.link_clicks) AS DOUBLE), 0) AS cvr
FROM daily_ad_metrics f
LEFT JOIN ads ad ON ad.ad_id = f.ad_id
LEFT JOIN ad_sets ad_set ON ad_set.ad_set_id = ad.ad_set_id
LEFT JOIN campaigns campaign ON campaign.campaign_id = ad_set.campaign_id
LEFT JOIN accounts account ON account.account_id = campaign.account_id
WHERE account.account_id = 'A3'
  AND f.date BETWEEN DATE '2026-01-01' AND DATE '2026-06-29';
