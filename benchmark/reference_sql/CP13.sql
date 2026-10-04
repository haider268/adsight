SELECT campaigns.name AS label, ROUND(SUM(m.clicks) / SUM(m.impressions), 4) AS ctr
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE ad_sets.name = 'Video Viewers'
  AND m.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-29'
GROUP BY 1
ORDER BY 1;
