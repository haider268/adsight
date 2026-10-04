SELECT date_trunc('month', m.date) AS period, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A3'
  AND m.date <= DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
