-- The current vehicles on trips of a date, by trip, freshest first: one per vehicle (two
-- sources' reports of one vehicle are one), its freshest report, every source reporting it,
-- its traits.
SELECT *
FROM (
    SELECT DISTINCT ON (v.trip_id, v.vehicle_id)
           v.trip_id, v.vehicle_id, v.status AS state, v.last_seen, v.latitude, v.longitude,
           v.bearing,
           array_agg(v.source) OVER (PARTITION BY v.trip_id, v.vehicle_id) AS sources,
           p.low_floor, p.air_conditioning, p.wheelchair_accessible, p.model, p.operator,
           p.licence_plate, p.fleet_number
    FROM rt.vehicle_state_current v
    LEFT JOIN rt.vehicle_profile p USING (vehicle_id)
    WHERE v.trip_id = ANY(%(trip_ids)s::text[]) AND v.service_date = %(service_date)s
        AND v.release_id = %(release_id)s AND v.last_seen >= %(seen_since)s
    ORDER BY v.trip_id, v.vehicle_id, v.last_seen DESC, v.source, v.source_vehicle_id
) found
ORDER BY trip_id, last_seen DESC, vehicle_id
