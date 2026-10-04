SELECT ad_sets.name AS label, ROUND(SUM(m.impressions) / SUM(m.reach), 3) AS frequency
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE ad_set_id IN ('S01', 'S02', 'S04')
  AND m.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-29'
GROUP BY 1
ORDER BY 1;
