SELECT campaigns.account_id AS label, ROUND(SUM(m.leads) / SUM(m.link_clicks), 4) AS cvr
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE m.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-29'
GROUP BY 1
ORDER BY 1;
