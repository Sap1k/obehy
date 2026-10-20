-- The current vehicles on a trip, freshest first.
SELECT v.source || ':' || v.source_vehicle_id AS vehicle_id, v.status AS state, v.last_seen,
       v.latitude, v.longitude, v.bearing
FROM rt.vehicle_state_current v
WHERE v.trip_id = %(trip_id)s AND v.service_date = %(service_date)s
    AND v.release_id = %(release_id)s AND v.last_seen >= %(seen_since)s
ORDER BY v.last_seen DESC, v.source, v.source_vehicle_id
