-- Polls per source channel since a minute (the last hour), for the stale-data banner.
SELECT source, channel, max(minute) AS last_minute, sum(polls)::integer AS polls,
       sum(errors)::integer AS errors, max(lag_ms) AS lag_ms
FROM rt.source_health
WHERE minute >= %(since)s
GROUP BY source, channel
ORDER BY source, channel
