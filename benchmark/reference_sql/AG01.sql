SELECT campaign_id, campaigns.name, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id)
JOIN campaigns USING (campaign_id)
WHERE m.date BETWEEN DATE '2026-05-31' AND DATE '2026-06-29'
GROUP BY campaign_id, campaigns.name
HAVING SUM(m.leads) > 0
ORDER BY cpl ASC, campaign_id;
