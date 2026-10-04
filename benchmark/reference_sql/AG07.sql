SELECT dayname(m.date) AS day, dayname(m.date) AS name, SUM(m.leads) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
GROUP BY 1
ORDER BY v ASC;
