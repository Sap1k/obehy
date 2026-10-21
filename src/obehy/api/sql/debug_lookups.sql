-- The lookups' recent upstream requests, newest first, and what is still pending.
SELECT 'fetch' AS kind, f.source || '/' || f.lookup AS lookup, f.cache_key AS subject,
       f.requested_at AS at, f.outcome, coalesce(f.error, f.status::text) AS detail,
       f.raw_sha256
FROM rt.lookup_fetch f
WHERE f.requested_at >= %(since)s
UNION ALL
SELECT 'request', r.kind, r.feed || ' ' || r.trip_id || ' ' || r.service_date || ' '
           || r.from_location,
       r.requested_at, CASE WHEN r.pending THEN 'pending' ELSE 'answered' END,
       r.outcomes::text, NULL
FROM rt.lookup_request r
WHERE r.requested_at >= %(since)s
ORDER BY at DESC
LIMIT %(limit)s
