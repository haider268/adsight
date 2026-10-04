SELECT strftime(f.date, '%Y-%m') AS key,
       strftime(f.date, '%Y-%m') AS label,
       SUM(f.impressions) AS value
FROM daily_ad_metrics f
WHERE f.date BETWEEN DATE '2026-01-01' AND DATE '2026-05-31'
  AND strftime(f.date, '%Y-%m') IN ('2026-01', '2026-02', '2026-03', '2026-04', '2026-05')
GROUP BY 1, 2
ORDER BY value ASC, key;
