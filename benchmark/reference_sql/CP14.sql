SELECT CASE WHEN dayofweek(m.date) IN (0, 6) THEN 'weekend' ELSE 'weekday' END AS label, ROUND(SUM(m.leads) / COUNT(DISTINCT m.date), 2) AS leads_per_day
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A2'
  AND m.date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
