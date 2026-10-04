SELECT campaign_id, campaigns.name, ROUND(SUM(m.spend) FILTER (WHERE month(m.date) = 5) - SUM(m.spend) FILTER (WHERE month(m.date) = 4), 2) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE m.date BETWEEN DATE '2026-04-01' AND DATE '2026-05-31'
GROUP BY 1, 2
ORDER BY v DESC, campaign_id;
