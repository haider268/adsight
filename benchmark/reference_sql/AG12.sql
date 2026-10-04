SELECT ad_set_id, ad_sets.name, ROUND(SUM(m.clicks) / SUM(m.impressions), 5) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A2'
  AND m.date BETWEEN DATE '2026-05-31' AND DATE '2026-06-29'
GROUP BY 1, 2
ORDER BY v ASC, ad_set_id;
