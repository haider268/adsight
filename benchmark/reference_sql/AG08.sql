SELECT ad_id, ads.name, SUM(m.link_clicks) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE campaigns.account_id = 'A3'
  AND m.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-29'
GROUP BY 1, 2
ORDER BY v DESC, ad_id;
