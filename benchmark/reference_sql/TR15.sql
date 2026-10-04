SELECT date_trunc('month', m.date) AS period, ROUND(SUM(m.spend), 2) AS spend
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A1'
  AND m.date BETWEEN DATE '2026-03-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
