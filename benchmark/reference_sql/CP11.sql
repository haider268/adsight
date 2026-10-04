SELECT ads.creative_type AS label, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A1'
GROUP BY 1
ORDER BY 1;
