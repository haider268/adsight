SELECT campaign_id, campaigns.name, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A1'
  AND m.date BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
GROUP BY 1, 2
HAVING SUM(m.leads) > 0
ORDER BY v DESC, campaign_id;
