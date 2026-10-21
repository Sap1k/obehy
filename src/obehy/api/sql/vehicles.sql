-- Current vehicles of the active release seen since a time, in some states, inside a viewport
-- or one vehicle: one row per vehicle (the worker resolves which vehicle each source report
-- is), its freshest report, every source reporting it, and its profile's traits. A vehicle's
-- trip is the feed trip it is on.
SELECT DISTINCT ON (v.vehicle_id)
       v.vehicle_id, v.feed, v.source, v.source_vehicle_id, v.status, v.last_seen, v.latitude,
       v.longitude, v.bearing, v.trip_id, v.service_date, t.route_id, r.short_name,
       r.long_name, r.mode, r.color, r.text_color, t.headsign, s.delay_s,
       array_agg(v.source) OVER (PARTITION BY v.vehicle_id) AS sources,
       p.low_floor, p.air_conditioning, p.wheelchair_accessible, p.model, p.operator,
       p.licence_plate, p.fleet_number
FROM rt.vehicle_state_current v
LEFT JOIN rt.vehicle_profile p USING (vehicle_id)
LEFT JOIN active.trip t ON t.trip_id = v.trip_id
LEFT JOIN active.route r ON r.route_id = t.route_id
LEFT JOIN rt.trip_state_current s
    ON (s.feed, s.key_namespace, s.key, s.service_date, s.release_id)
        = (v.feed, v.key_namespace, v.key, v.service_date, v.release_id)
WHERE v.release_id = %(release_id)s AND v.last_seen >= %(seen_since)s
    AND v.status = ANY(%(states)s::text[])
    AND (%(west)s::double precision IS NULL OR (
        v.longitude BETWEEN %(west)s AND %(east)s AND v.latitude BETWEEN %(south)s AND %(north)s
    ))
    AND (%(vehicle_id)s::text IS NULL OR v.vehicle_id = %(vehicle_id)s)
ORDER BY v.vehicle_id, v.last_seen DESC, v.source, v.source_vehicle_id, v.feed
LIMIT %(limit)s
