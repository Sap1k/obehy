-- Polls per source channel over a window, summed.
SELECT source, channel, min(minute) AS first_minute, max(minute) AS last_minute,
       sum(polls)::integer AS polls, sum(errors)::integer AS errors, sum(rows)::integer AS rows,
       max(lag_ms) AS max_lag_ms, avg(lag_ms)::float AS mean_lag_ms
FROM rt.source_health
WHERE minute >= %(since)s
GROUP BY source, channel
ORDER BY source, channel
