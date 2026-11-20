-- The current vehicles on trips, each on its service date (`trip_ids` and `service_dates`
-- pairwise), by trip, the one standing for the journey first (emit/represent.py), then freshest
-- first: one per vehicle (two sources' reports of one vehicle are one), its report standing for
-- the journey else its newest fix, every source reporting it and whether its ID is a vehicle on
-- the trip's mode, its traits. Boards, trips, lines and enriched legs show the first of a trip.
SELECT *
FROM (
    SELECT DISTINCT ON (v.trip_id, v.service_date, v.vehicle_id)
           v.trip_id, v.service_date, v.vehicle_id, v.status AS state, v.last_seen,
           v.position_at, v.latitude, v.longitude, v.bearing,
           bool_or(v.represents) OVER vehicle AS represents,
           array_agg(v.source) OVER vehicle AS sources,
           array_agg(v.source_vehicle_id) OVER vehicle AS source_vehicle_ids,
           array_agg(rt.is_physical(v.source, ro.mode)) OVER vehicle AS physical,
           p.low_floor, p.air_conditioning, p.wheelchair_accessible, p.manufacturer, p.model,
           p.operator, p.licence_plate, p.fleet_number
    FROM rt.vehicle_state_current v
    LEFT JOIN rt.vehicle_profile p USING (vehicle_id)
    LEFT JOIN active.trip t ON t.trip_id = v.trip_id
    LEFT JOIN active.route ro ON ro.route_id = t.route_id
    WHERE (v.trip_id, v.service_date) IN (
            SELECT * FROM unnest(%(trip_ids)s::text[], %(service_dates)s::date[])
        )
        AND v.release_id = %(release_id)s AND v.last_seen >= %(seen_since)s
    WINDOW vehicle AS (PARTITION BY v.trip_id, v.service_date, v.vehicle_id)
    ORDER BY v.trip_id, v.service_date, v.vehicle_id, v.latitude IS NULL, v.represents DESC,
        v.position_at DESC NULLS LAST, v.last_seen DESC, v.source, v.source_vehicle_id
) found
ORDER BY trip_id, service_date, represents DESC, last_seen DESC, vehicle_id
