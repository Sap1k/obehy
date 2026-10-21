-- The current state of the journey publishing the trip, in the active release, with the sources
-- its realtime came from.
SELECT s.lifecycle, s.delay_s, s.off_route, s.stale, s.updated_at, s.sources
FROM rt.trip_part_current p
JOIN rt.trip_state_current s USING (feed, key_namespace, key, service_date, release_id)
WHERE p.trip_id = %(trip_id)s AND p.service_date = %(service_date)s
    AND p.release_id = %(release_id)s
ORDER BY s.updated_at DESC
LIMIT 1
