-- The current state of the journey publishing the trip, in the active release, with the sources
-- its realtime came from (rt.trip_current).
SELECT lifecycle, delay_s, off_route, stale, updated_at, sources
FROM rt.trip_current(%(trip_id)s, %(service_date)s, %(release_id)s)
