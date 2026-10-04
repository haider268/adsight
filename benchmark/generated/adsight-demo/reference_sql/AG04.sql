SELECT dayname(f.date) AS key,
       dayname(f.date) AS label,
       SUM(f.leads) AS value
FROM daily_ad_metrics f
WHERE f.date BETWEEN DATE '2026-01-01' AND DATE '2026-06-29'
GROUP BY 1, 2
ORDER BY value ASC, key;
