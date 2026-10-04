SELECT date_trunc('week', m.date) AS period, SUM(m.impressions) AS impressions
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaign_id = 'C4'
  AND m.date BETWEEN DATE '2026-02-02' AND DATE '2026-03-01'
GROUP BY 1
ORDER BY 1;
