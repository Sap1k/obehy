-- Each realtime source's newest vehicle report in the active release: the worker rewrites
-- these every tick, so a stalled worker or a silent source shows here.
SELECT source, max(last_seen) AS last_seen
FROM rt.vehicle_state_current
WHERE release_id = %(release_id)s
GROUP BY source
ORDER BY source
