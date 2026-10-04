SELECT SUM(m.leads) AS leads
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A2'
  AND m.date BETWEEN DATE '2026-04-01' AND DATE '2026-04-30'
  AND dayofweek(m.date) IN (0, 6);
