SELECT ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A3'
  AND m.date BETWEEN DATE '2026-04-01' AND DATE '2026-04-30';
