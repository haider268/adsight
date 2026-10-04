SELECT date_trunc('week', m.date) AS week, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id)
WHERE ad_sets.campaign_id = 'C4'
  AND m.date BETWEEN DATE '2026-06-01' AND DATE '2026-06-28'
GROUP BY week ORDER BY week;
