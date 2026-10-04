SELECT ad_set_id, ad_sets.name, SUM(m.leads) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
GROUP BY 1, 2
ORDER BY v DESC, ad_set_id;
