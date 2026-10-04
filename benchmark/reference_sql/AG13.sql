SELECT campaigns.account_id, accounts.name, ROUND(SUM(m.leads) / SUM(m.link_clicks), 5) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id) JOIN accounts USING (account_id)
WHERE m.date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31'
GROUP BY 1, 2
ORDER BY v DESC;
