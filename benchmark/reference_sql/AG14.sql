SELECT strftime(m.date, '%Y-%m') AS month, strftime(m.date, '%Y-%m') AS name, ROUND(SUM(m.spend), 2) AS v
FROM daily_ad_metrics m JOIN ads USING (ad_id) JOIN ad_sets USING (ad_set_id) JOIN campaigns USING (campaign_id)
WHERE m.date <= DATE '2026-05-31'
GROUP BY 1
ORDER BY v DESC;
