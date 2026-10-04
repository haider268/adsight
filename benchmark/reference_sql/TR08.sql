SELECT date_trunc('month', m.date) AS period, SUM(m.impressions) AS impressions
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaign_id = 'C6'
  AND m.date BETWEEN DATE '2026-03-01' AND DATE '2026-05-31'
GROUP BY 1
ORDER BY 1;
