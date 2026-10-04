SELECT campaign_id, campaigns.name, SUM(m.leads) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE m.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-29'
GROUP BY 1, 2
ORDER BY v ASC, campaign_id;
