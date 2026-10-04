SELECT date_trunc('week', m.date) AS period, SUM(m.leads) AS leads
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A3'
  AND m.date BETWEEN DATE '2026-05-04' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
