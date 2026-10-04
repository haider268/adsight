SELECT campaigns.account_id AS label, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE m.date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
