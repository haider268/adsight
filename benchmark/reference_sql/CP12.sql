SELECT ad_set_id || ' ' || name AS label, daily_budget
FROM ad_sets
WHERE campaign_id IN ('C7', 'C8')
ORDER BY 1;
