SELECT ad_sets.placement AS label, ROUND(SUM(m.clicks) / SUM(m.impressions), 4) AS ctr
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
GROUP BY 1
ORDER BY 1;
