-- Current vehicles of the active release seen since a time, in some states, inside a viewport
-- or one vehicle: one row per vehicle (the worker resolves which vehicle each source report
-- is), every source reporting it, and its profile's traits. A vehicle's trip is the feed trip
-- it is on.
--
-- A vehicle two sources report may be bound differently by each (DÚK and PID disagreeing about
-- its next trip): its trip, state and delay come from its best-ranked report (one standing for
-- its journey, then the state, then the source), so they do not flip with whichever report is
-- fresher; its position comes from its freshest report. A viewport lists only vehicles standing
-- for their journey (emit/represent.py): the SŽ map's train and the unit's GPS are one train.
WITH reports AS (
    SELECT v.*, array_agg(v.source) OVER (PARTITION BY v.vehicle_id) AS sources
    FROM rt.vehicle_state_current v
    WHERE v.release_id = %(release_id)s AND v.last_seen >= %(seen_since)s
        AND (%(vehicle_id)s::text IS NULL OR v.vehicle_id = %(vehicle_id)s)
),
binding AS (
    SELECT DISTINCT ON (r.vehicle_id)
           r.vehicle_id, r.feed, r.source, r.source_vehicle_id, r.status, r.key_namespace, r.key,
           r.service_date, r.trip_id, r.represents, r.sources
    FROM reports r
    ORDER BY r.vehicle_id, r.represents DESC,
        array_position(
            ARRAY['running', 'positioning', 'layover', 'unmatched', 'not_in_service'], r.status
        ),
        r.source, r.source_vehicle_id, r.feed
),
fix AS (
    SELECT DISTINCT ON (r.vehicle_id)
           r.vehicle_id, r.last_seen, r.latitude, r.longitude, r.bearing
    FROM reports r
    ORDER BY r.vehicle_id, r.last_seen DESC, r.source, r.source_vehicle_id, r.feed
)
SELECT b.vehicle_id, b.feed, b.source, b.source_vehicle_id, b.status, f.last_seen, f.latitude,
       f.longitude, f.bearing, b.trip_id, b.service_date, t.route_id, ro.short_name,
       ro.long_name, ro.mode, ro.color, ro.text_color, t.headsign, s.delay_s, b.sources,
       p.low_floor, p.air_conditioning, p.wheelchair_accessible, p.model, p.operator,
       p.licence_plate, p.fleet_number
FROM binding b
JOIN fix f USING (vehicle_id)
LEFT JOIN rt.vehicle_profile p USING (vehicle_id)
LEFT JOIN active.trip t ON t.trip_id = b.trip_id
LEFT JOIN active.route ro ON ro.route_id = t.route_id
LEFT JOIN rt.trip_state_current s
    ON (s.feed, s.key_namespace, s.key, s.service_date, s.release_id)
        = (b.feed, b.key_namespace, b.key, b.service_date, %(release_id)s)
WHERE b.status = ANY(%(states)s::text[])
    AND (%(vehicle_id)s::text IS NOT NULL OR b.represents)
    AND (%(west)s::double precision IS NULL OR (
        f.longitude BETWEEN %(west)s AND %(east)s AND f.latitude BETWEEN %(south)s AND %(north)s
    ))
ORDER BY b.vehicle_id
LIMIT %(limit)s
