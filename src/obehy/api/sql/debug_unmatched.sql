-- Recent observations the core did not bind, by source, channel and reason, with examples.
SELECT source, channel, feed, reason, count(*)::integer AS observations,
       max(received_at) AS last_received,
       (array_agg(facts ORDER BY received_at DESC))[1:3] AS examples
FROM rt.observation
WHERE received_at >= %(since)s AND reason IS NOT NULL
GROUP BY source, channel, feed, reason
ORDER BY observations DESC, source, channel, reason
LIMIT %(limit)s
