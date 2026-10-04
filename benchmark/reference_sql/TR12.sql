SELECT date_trunc('month', m.date) AS period, ROUND(SUM(m.clicks) / SUM(m.impressions), 4) AS ctr
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaign_id = 'C2'
GROUP BY 1
ORDER BY 1;
