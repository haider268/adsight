SELECT SUM(m.link_clicks) AS link_clicks
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE ad_set_id = 'S10'
  AND m.date BETWEEN DATE '2026-05-01' AND DATE '2026-05-31';
