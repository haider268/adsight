SELECT CASE WHEN m.date < DATE '2026-03-16' THEN '1-15 March' ELSE '16-31 March' END AS label, ROUND(SUM(m.spend) / SUM(m.leads), 2) AS cpl
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE ad_set_id = 'S07'
  AND m.date BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
GROUP BY 1
ORDER BY 1;
